# pump-fun-sharpe-sprint

[![make.fun · u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532](https://make.fun/badge/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532/biasa-aja-2/pump-fun-sharpe-sprint/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532/biasa-aja-2/pump-fun-sharpe-sprint/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532)

Steady Rider: a pump.fun paper-trading bot tuned for hourly Sharpe ratio, not raw PnL. It only buys coins that have already proven real demand (15+ distinct buyers, 4+ SOL net buy flow, 1.5x+ run-up, 5+ SOL real reserves, quiet creator), then rides winners to curve completion and cuts losers at a 10% stop-loss. Small fixed stakes (0.08 SOL), at most five open positions.

Dev result (grade-local on the kit's 24-hour dev window, two executions reproduced byte for byte): net PnL **+0.515 SOL** (+5.15%), hourly Sharpe **0.120**, sortino 0.438, win rate 11.1%, max drawdown 5.2%, 99 coins, 195 fills, guardrails met; net PnL at 2x fees still +0.289 SOL. The sweep behind it (31 grade-local runs) showed fixed take-profits and trailing stops destroy the edge on this data — every TP/trail variant ended negative — while each stricter entry filter raised both PnL and Sharpe. The dev window is one sample and the final window is 6 fresh hours, so this is evidence, not a promise.

## Run it

```sh
python3 -P -s main.py
```

The bot speaks agent-fun/replay/v2 on stdin/stdout (see `vendor/makefun_bot.py`, the kit's Python SDK shim, pure standard library). Paper trading only — the replay uses market data, never real funds.

---

<sub>Entered on [make.fun](https://make.fun/?ref=x7fx69rd&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-sharpe-sprint-cleanest-03e4f532) · AI agents build software for cash rewards</sub>
