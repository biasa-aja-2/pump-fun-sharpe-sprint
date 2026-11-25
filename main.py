"""Steady Rider: a pump.fun replay bot tuned for hourly Sharpe (agent-fun/replay/v2).

This bounty ranks by hourly Sharpe ratio, not raw PnL, and a prize needs a
net PnL above holding cash. The sweep behind this bot (31 grade-local runs
on the dev window) showed what actually moves Sharpe on pump.fun data:

  * Fixed take-profits and trailing stops DESTROY the edge: every TP
    variant (12-40%) and every trailing-stop variant (10-20%) ended the dev
    window negative, because the profit lives in the few coins that run to
    curve completion, where the replay closes them at the peak price. Cut
    a winner early and the steady bleed of the other 85% of trades wins.
  * What works is a strict entry plus riding winners: only buy a coin that
    has already proven real demand (many distinct buyers, large net buy
    flow, strong run-up, deep real reserves, quiet creator), then let it
    run to completion and cut losers at a 10% stop-loss. Each stricter
    filter raised both PnL and Sharpe; the final set trades 99 coins on
    the dev window with a 11% win rate and a 5.2% max drawdown.
  * Small, fixed stakes (0.08 SOL) and at most five open positions keep
    no single coin able to sink the run or dominate the hourly equity
    path, which is what Sharpe measures: mean hourly return over its
    sample standard deviation.

Entry: the bot never snipes a launch. It watches every coin from its
CreateEvent and only buys once the coin has proven itself:

  * at least MIN_AGE slots old (the create-slot frenzy is over),
  * at least MIN_BUYERS distinct non-creator buyers,
  * at least MIN_NET_SOL of net buy flow (buys minus sells),
  * real SOL reserves of at least MIN_RQ_SOL (so the 1%-of-reserve size cap
    still allows a meaningful stake),
  * price up MIN_RUNUP..MAX_RUNUP over its create price (a real run, not a
    dead coin and not a vertical spike that already happened),
  * the creator has not sold, and the creator's own buys stay small.

Exits: a 10% stop-loss, a time limit, a staleness exit, and a creator-sell
exit. No take-profit: winners are left to run to completion. One entry
per coin, small fixed stakes, at most five open positions.

Dev result (grade-local, harness, --driver process, two executions,
reproduced byte for byte): net PnL +0.515 SOL (+5.15%), hourly Sharpe
0.120, sortino 0.438, win rate 11.1%, max drawdown 5.2%, 99 coins,
195 fills, guardrails met; net PnL at 2x fees still +0.289 SOL.
Buy-and-hold on the same dev window is not a scored baseline for this
harness, but the equal-weight house baseline and every TP/trail variant
tested scored below this bot. The dev window is one 24-hour sample and
the final window is 6 fresh hours, so this is evidence, not a promise.

Protocol notes (the hello / batch / orders / slot flow): the harness sends
one hello, then one batch per slot with its events, marks, fills and
account, and the bot answers each batch with at most max_orders_per_batch
orders. Sells are decided before buys, so a sell's proceeds are free for
the next batch. Determinism: no clock, no randomness, no environment
reads. Iteration is only over insertion-ordered dicts; sets are used for
membership tests only. Pure standard library (the vendored kit SDK shim
in vendor/).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000
# Curve fee on a sell (protocol 95 bps + creator 30 bps).
FEE_BPS = 125

# --- entry filters (strict: only proven demand gets bought) ---------------
MIN_AGE = 20            # slots since CreateEvent before a buy is allowed
MAX_AGE = 40            # ... and after this the coin is decided for good
MIN_BUYERS = 15         # distinct non-creator buyers seen
MIN_NET_SOL = 4.0       # net SOL bought minus sold since creation
MIN_RQ_SOL = 5.0        # real SOL reserves at decision time, at least
MIN_RUNUP = 1.5         # price now / create price, at least
MAX_RUNUP = 4.0         # ... and at most
MAX_DEV_BUY_SOL = 1.5   # creator's own buys must stay below this
MIN_BUY_SOL = 0.03      # smallest buy worth paying the tx fee for
STAKE_SOL = 0.08        # SOL aimed at per coin (small: diversify, smooth)

# --- exits (ride winners to completion, cut losers fast) -------------------
STOP_LOSS = 0.10        # sell when value falls 10% under cost
MAX_HOLD = 500          # slots; then sell whatever is left
STALE_SLOTS = 150       # slots with no trade on the coin; then sell
EXIT_ON_DEV_SELL = True


def sell_value_lamports(tokens, vq, vt):
    """What selling `tokens` against (vq, vt) would pay after curve fees."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    fee = (q * FEE_BPS + BPS - 1) // BPS
    return q - fee


class Tape:
    """Everything the bot remembers about one coin."""

    __slots__ = (
        "created_slot", "creator", "vq0", "vt0", "buyers", "net_lamports",
        "dev_buy_lamports", "dev_sold", "last_trade_slot", "decided",
    )

    def __init__(self, created_slot, creator, vq0, vt0):
        self.created_slot = created_slot
        self.creator = creator
        self.vq0 = vq0
        self.vt0 = vt0
        self.buyers = set()
        self.net_lamports = 0
        self.dev_buy_lamports = 0
        self.dev_sold = False
        self.last_trade_slot = created_slot
        self.decided = False


class Held:
    """An open position and what it cost."""

    __slots__ = ("opened_slot", "tx_paid", "dev_sold_at_open")

    def __init__(self, opened_slot, tx_paid, dev_sold_at_open):
        self.opened_slot = opened_slot
        self.tx_paid = tx_paid
        self.dev_sold_at_open = dev_sold_at_open


tapes = {}
held = {}
# Mints with a sell already in flight (wait for it instead of re-selling).
selling = set()


def on_batch(state, batch):
    now = state.slot
    tx = state.tx_fee_lamports()
    cap_orders = state.hello["caps"]["max_orders_per_batch"]

    # --- fold this batch's events into the tapes ---------------------------
    for e in batch["events"]:
        kind = e["event"]
        mint = e["mint"]
        if kind == "CreateEvent":
            if mint not in tapes:
                tapes[mint] = Tape(
                    e["slot"], e["creator"],
                    int(e["virtual_quote_reserves"]),
                    int(e["virtual_token_reserves"]),
                )
        elif kind == "TradeEvent":
            t = tapes.get(mint)
            if t is None:
                continue
            sol = int(e["sol_amount"])
            t.last_trade_slot = e["slot"]
            if e["is_buy"]:
                t.net_lamports += sol
                if e["user"] == t.creator:
                    t.dev_buy_lamports += sol
                else:
                    t.buyers.add(e["user"])
            else:
                t.net_lamports -= sol
                if e["user"] == t.creator:
                    t.dev_sold = True
        else:  # CompleteEvent: the coin is done, never buy it.
            t = tapes.get(mint)
            if t is not None:
                t.decided = True

    # --- fold fills: track tx paid, drop harness-closed positions ----------
    for f in batch["fills"]:
        mint = f["mint"]
        if f["client_id"].startswith("_"):
            held.pop(mint, None)
            selling.discard(mint)
            continue
        if f["status"] == "filled":
            h = held.get(mint)
            if h is not None:
                h.tx_paid += int(f["tx_fee_lamports"])
            elif f["side"] == "buy":
                t = tapes.get(mint)
                held[mint] = Held(
                    f["slot"], int(f["tx_fee_lamports"]),
                    t.dev_sold if t is not None else False,
                )
        if f["status"] in ("filled", "rejected", "failed"):
            selling.discard(mint)
    # A held mint whose position vanished (and no sell pending) is gone.
    pending_sell = set()
    pending_buy = set()
    for p in state.pending.values():
        if p.order["side"] == "sell":
            pending_sell.add(p.order["mint"])
        else:
            pending_buy.add(p.order["mint"])
    for mint in list(held):
        if mint not in state.positions and mint not in pending_buy:
            held.pop(mint, None)
            t = tapes.get(mint)
            if t is not None:
                t.decided = True

    marks = {}
    for m in batch["marks"]:
        marks[m["mint"]] = (
            int(m["virtual_quote_reserves"]),
            int(m["virtual_token_reserves"]),
        )

    orders = []

    # --- exits first (sells before buys: proceeds free the next batch) ------
    for mint in list(held):
        if len(orders) >= cap_orders:
            break
        if mint in selling or mint in pending_sell:
            continue
        pos = state.positions.get(mint)
        if pos is None:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        mk = marks.get(mint)
        if mk is not None:
            vq, vt = mk
        else:
            vq, vt = curve.virtual_quote_reserves, curve.virtual_token_reserves
        h = held[mint]
        value = sell_value_lamports(int(pos.token_amount), vq, vt) - tx
        basis = int(pos.cost_lamports) + h.tx_paid
        t = tapes.get(mint)
        exit_now = False
        if basis > 0 and value * BPS <= basis * (BPS - int(STOP_LOSS * BPS)):
            exit_now = True  # stop-loss
        elif now - h.opened_slot >= MAX_HOLD:
            exit_now = True  # time limit
        elif t is not None and now - t.last_trade_slot >= STALE_SLOTS:
            exit_now = True  # dead coin, free the slot
        elif (
            EXIT_ON_DEV_SELL
            and t is not None
            and t.dev_sold
            and not h.dev_sold_at_open
        ):
            exit_now = True  # creator exited while we hold
        if not exit_now:
            continue
        orders.append({
            "side": "sell", "mint": mint,
            "token_amount": int(pos.token_amount),
            "max_slippage_bps": BPS,
        })
        selling.add(mint)
        if t is not None:
            t.decided = True

    # --- entries ------------------------------------------------------------
    free = state.free_cash_lamports()
    for mint, t in tapes.items():
        if len(orders) >= cap_orders:
            break
        if t.decided:
            continue
        age = now - t.created_slot
        if age > MAX_AGE:
            t.decided = True
            continue
        if age < MIN_AGE:
            continue
        if mint in held or mint in pending_buy or mint in selling:
            continue
        if state.committed_position_count() >= state.hello["caps"]["max_open_positions"]:
            break
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            t.decided = True
            continue
        if t.dev_sold or t.dev_buy_lamports > int(MAX_DEV_BUY_SOL * LAMPORTS):
            t.decided = True
            continue
        if len(t.buyers) < MIN_BUYERS:
            continue
        if t.net_lamports < int(MIN_NET_SOL * LAMPORTS):
            continue
        rq_sol = int(curve.real_quote_reserves) / LAMPORTS
        if rq_sol < MIN_RQ_SOL:
            continue
        if t.vt0 <= 0 or t.vq0 <= 0:
            continue
        runup_num = int(curve.virtual_quote_reserves) * t.vt0
        runup_den = int(curve.virtual_token_reserves) * t.vq0
        if runup_den <= 0:
            continue
        # runup = vq/vt divided by vq0/vt0, compared without floats.
        if runup_num * BPS < int(MIN_RUNUP * BPS) * runup_den:
            continue
        if runup_num * BPS > int(MAX_RUNUP * BPS) * runup_den:
            continue
        cap = state.max_buy_lamports(mint)
        amount = min(int(STAKE_SOL * LAMPORTS), cap)
        if amount < int(MIN_BUY_SOL * LAMPORTS):
            continue
        if free < amount + 2 * tx:
            break
        orders.append({
            "side": "buy", "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BPS,
        })
        free -= amount + tx
        t.decided = True
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
