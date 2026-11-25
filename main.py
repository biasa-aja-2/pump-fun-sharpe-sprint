"""Steady Rider — a pump.fun paper bot that chases hourly Sharpe, not raw PnL.

Ranking here is by hourly Sharpe ratio, and a prize additionally needs a
net PnL above holding cash. Thirty-one grade-local runs on the dev window
taught this bot what actually moves Sharpe on pump.fun tapes:

* Capping winners kills the edge. Every fixed take-profit tried (12%,
  20%, 25%, 30%, 40%) and every trailing stop tried (10%, 12%, 15%, 20%)
  finished the dev window negative. The profit lives in the handful of
  coins that run all the way to curve completion, where the replay closes
  them at the peak price. Exit one early and the slow bleed of the other
  ~85% of trades — each one a small loss — wins the day.
* Selectivity is the whole game. Each stricter entry filter raised both
  PnL and Sharpe: more distinct buyers, more net buy flow, a higher
  run-up, deeper real reserves, a quieter creator. The final filter set
  buys 99 of the dev window's 1,222 coins and wins 11% of its round
  trips, with a 5.2% maximum drawdown.
* Stakes stay small and fixed (0.08 SOL) with at most five positions
  open, so no single coin can sink the run or dominate one hourly
  equity mark — and the hourly equity marks are exactly what Sharpe is
  computed from: mean hourly return over its sample standard deviation.

How it trades: never a launch snipe. The bot follows every coin from its
CreateEvent and buys only after the coin has shown genuine demand — old
enough that the creation frenzy is over, bought by many distinct wallets
other than the creator, carrying several SOL of net buy flow, sitting on
deep real reserves, up a healthy multiple from its creation price but not
a vertical spike, with a creator who has not sold and has not bought much
either. Winners are then left alone until the curve completes; losers are
cut by a 10% stop-loss, a hold-time cap, a staleness rule, or a creator
exit. One buy per coin, ever.

Dev window score (grade-local, --driver process, the bot executed twice
and the two action logs matched byte for byte): net PnL +0.515 SOL
(+5.15%), hourly Sharpe 0.120, sortino 0.438, win rate 11.1%, max
drawdown 5.2%, 99 coins, 195 fills, every guardrail met; at doubled fees
the net PnL is still +0.289 SOL. The dev window is a single 24-hour
sample and the final window is six fresh hours, so treat this as
evidence, not a promise. A --stress 10 check underlines the fragility:
the median rebuilt copy lost 0.391 SOL, nine of ten copies lost money,
and the three best coins made 82% of the dev gains (without them the net
PnL is -0.941 SOL). The entry below is the best variant found, but a
fresh window may well score near or below zero; no placing is promised.

Wire protocol in one paragraph (hello, batch, orders, slot): the harness
opens with a single hello, then sends one batch per slot carrying that
slot's events, reserve marks, fills and account snapshot, and the bot
answers every batch with at most max_orders_per_batch orders. Exits are
chosen before entries, so a sale's proceeds are available to the next
batch rather than the current one. The run is fully deterministic — no
clock, no randomness, no environment reads — and every loop walks an
insertion-ordered dict while sets serve only membership tests. Standard
library only; the kit's Python SDK shim lives in vendor/.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000
# What a sell pays in curve fees: protocol 95 bps plus creator 30 bps.
SELL_FEE_BPS = 125

# --- When a coin earns a buy: strict, all of these at once ----------------
EARLIEST_SLOT = 20      # slots after CreateEvent before buying is allowed
LATEST_SLOT = 40        # ... and past this the coin is settled for good
NEED_BUYERS = 15        # distinct non-creator buyers observed
NEED_NET_SOL = 4.0      # net SOL bought minus sold since creation
NEED_REAL_SOL = 5.0     # real SOL reserves when the decision is made
NEED_RUNUP = 1.5        # price now over creation price, lower bound
MAX_RUNUP = 4.0         # ... and upper bound
MAX_MAKER_BUY_SOL = 1.5  # the creator's own buys must stay under this
SMALLEST_BUY_SOL = 0.03  # a buy smaller than this is not worth its tx fee
STAKE_SOL = 0.08        # SOL per coin: small on purpose, for diversification

# --- When a position is closed --------------------------------------------
STOP_PCT = 0.10         # sell once value drops 10% below cost
HOLD_LIMIT = 500        # slots held, then sell whatever remains
IDLE_LIMIT = 150        # slots without a trade, then sell and free the slot
EXIT_IF_MAKER_SELLS = True


def proceeds_for(amount_tokens, quote_res, token_res):
    """SOL a sell of `amount_tokens` returns after curve fees, in lamports."""
    if amount_tokens <= 0 or token_res <= 0:
        return 0
    gross = (amount_tokens * quote_res) // (token_res + amount_tokens)
    fee = (gross * SELL_FEE_BPS + BPS - 1) // BPS
    return gross - fee


class CoinBook:
    """One coin's tape: what the bot has learned about it since creation."""

    __slots__ = (
        "born_slot", "maker", "quote0", "token0", "buyer_set", "net_flow",
        "maker_bought", "maker_sold", "last_seen_slot", "settled",
    )

    def __init__(self, born_slot, maker, quote0, token0):
        self.born_slot = born_slot
        self.maker = maker
        self.quote0 = quote0
        self.token0 = token0
        self.buyer_set = set()
        self.net_flow = 0
        self.maker_bought = 0
        self.maker_sold = False
        self.last_seen_slot = born_slot
        self.settled = False


class OpenTrade:
    """One open position: when it filled and what it has cost so far."""

    __slots__ = ("filled_slot", "fees_paid", "maker_sold_at_fill")

    def __init__(self, filled_slot, fees_paid, maker_sold_at_fill):
        self.filled_slot = filled_slot
        self.fees_paid = fees_paid
        self.maker_sold_at_fill = maker_sold_at_fill


books = {}
open_trades = {}
# Mints whose sell order is already in flight: wait for its fill.
sell_in_flight = set()


def on_batch(state, batch):
    slot_now = state.slot
    tx_fee = state.tx_fee_lamports()
    order_cap = state.hello["caps"]["max_orders_per_batch"]

    # --- Learn from this batch's events ------------------------------------
    for event in batch["events"]:
        kind = event["event"]
        mint = event["mint"]
        if kind == "CreateEvent":
            if mint not in books:
                books[mint] = CoinBook(
                    event["slot"], event["creator"],
                    int(event["virtual_quote_reserves"]),
                    int(event["virtual_token_reserves"]),
                )
        elif kind == "TradeEvent":
            book = books.get(mint)
            if book is None:
                continue
            moved = int(event["sol_amount"])
            book.last_seen_slot = event["slot"]
            if event["is_buy"]:
                book.net_flow += moved
                if event["user"] == book.maker:
                    book.maker_bought += moved
                else:
                    book.buyer_set.add(event["user"])
            else:
                book.net_flow -= moved
                if event["user"] == book.maker:
                    book.maker_sold = True
        else:  # CompleteEvent: the curve is finished, never buy this coin.
            book = books.get(mint)
            if book is not None:
                book.settled = True

    # --- Learn from this batch's fills --------------------------------------
    for fill in batch["fills"]:
        mint = fill["mint"]
        if fill["client_id"].startswith("_"):
            # A harness close (completion, flatten, write-off): position gone.
            open_trades.pop(mint, None)
            sell_in_flight.discard(mint)
            continue
        if fill["status"] == "filled":
            trade = open_trades.get(mint)
            if trade is not None:
                trade.fees_paid += int(fill["tx_fee_lamports"])
            elif fill["side"] == "buy":
                book = books.get(mint)
                open_trades[mint] = OpenTrade(
                    fill["slot"], int(fill["tx_fee_lamports"]),
                    book.maker_sold if book is not None else False,
                )
        if fill["status"] in ("filled", "rejected", "failed"):
            sell_in_flight.discard(mint)
    # A tracked trade whose position is gone (and no buy pending) is gone.
    sell_pending = set()
    buy_pending = set()
    for awaiting in state.pending.values():
        if awaiting.order["side"] == "sell":
            sell_pending.add(awaiting.order["mint"])
        else:
            buy_pending.add(awaiting.order["mint"])
    for mint in list(open_trades):
        if mint not in state.positions and mint not in buy_pending:
            open_trades.pop(mint, None)
            book = books.get(mint)
            if book is not None:
                book.settled = True

    latest_marks = {}
    for mark in batch["marks"]:
        latest_marks[mark["mint"]] = (
            int(mark["virtual_quote_reserves"]),
            int(mark["virtual_token_reserves"]),
        )

    orders = []

    # --- Exits first: a sale's cash is spendable from the next batch on ----
    for mint in list(open_trades):
        if len(orders) >= order_cap:
            break
        if mint in sell_in_flight or mint in sell_pending:
            continue
        holding = state.positions.get(mint)
        if holding is None:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        mark = latest_marks.get(mint)
        if mark is not None:
            quote_res, token_res = mark
        else:
            quote_res, token_res = (
                curve.virtual_quote_reserves,
                curve.virtual_token_reserves,
            )
        trade = open_trades[mint]
        worth = proceeds_for(int(holding.token_amount), quote_res, token_res) - tx_fee
        cost = int(holding.cost_lamports) + trade.fees_paid
        book = books.get(mint)
        reason = None
        if cost > 0 and worth * BPS <= cost * (BPS - int(STOP_PCT * BPS)):
            reason = "stop-loss"
        elif slot_now - trade.filled_slot >= HOLD_LIMIT:
            reason = "hold limit"
        elif book is not None and slot_now - book.last_seen_slot >= IDLE_LIMIT:
            reason = "idle coin"
        elif (
            EXIT_IF_MAKER_SELLS
            and book is not None
            and book.maker_sold
            and not trade.maker_sold_at_fill
        ):
            reason = "maker exited"
        if reason is None:
            continue
        orders.append({
            "side": "sell", "mint": mint,
            "token_amount": int(holding.token_amount),
            "max_slippage_bps": BPS,
        })
        sell_in_flight.add(mint)
        if book is not None:
            book.settled = True

    # --- Entries: only coins that have proven themselves --------------------
    cash_free = state.free_cash_lamports()
    for mint, book in books.items():
        if len(orders) >= order_cap:
            break
        if book.settled:
            continue
        age = slot_now - book.born_slot
        if age > LATEST_SLOT:
            book.settled = True
            continue
        if age < EARLIEST_SLOT:
            continue
        if mint in open_trades or mint in buy_pending or mint in sell_in_flight:
            continue
        if state.committed_position_count() >= state.hello["caps"]["max_open_positions"]:
            break
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            book.settled = True
            continue
        if book.maker_sold or book.maker_bought > int(MAX_MAKER_BUY_SOL * LAMPORTS):
            book.settled = True
            continue
        if len(book.buyer_set) < NEED_BUYERS:
            continue
        if book.net_flow < int(NEED_NET_SOL * LAMPORTS):
            continue
        real_sol = int(curve.real_quote_reserves) / LAMPORTS
        if real_sol < NEED_REAL_SOL:
            continue
        if book.token0 <= 0 or book.quote0 <= 0:
            continue
        # runup = (quote_res/token_res) / (quote0/token0), cross-multiplied.
        runup_top = int(curve.virtual_quote_reserves) * book.token0
        runup_bottom = int(curve.virtual_token_reserves) * book.quote0
        if runup_bottom <= 0:
            continue
        if runup_top * BPS < int(NEED_RUNUP * BPS) * runup_bottom:
            continue
        if runup_top * BPS > int(MAX_RUNUP * BPS) * runup_bottom:
            continue
        size_cap = state.max_buy_lamports(mint)
        amount = min(int(STAKE_SOL * LAMPORTS), size_cap)
        if amount < int(SMALLEST_BUY_SOL * LAMPORTS):
            continue
        if cash_free < amount + 2 * tx_fee:
            break
        orders.append({
            "side": "buy", "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BPS,
        })
        cash_free -= amount + tx_fee
        book.settled = True
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
