# 🤖 Autonomous MT5 Trading Bot — Gold & BTC

A fully autonomous, 24/7 trading system that connects to **MetaTrader 5**,
trades **XAUUSD (gold)** and **BTCUSD** on the **1m / 3m / 5m** timeframes using
a multi-indicator confluence strategy, sizes positions from your balance,
and aggressively **protects profits** with break-even, partial take-profit and
ATR trailing stops. It ships with a **live web dashboard**, a **news/economic
calendar filter**, and a **backtester** that replays the exact same logic over
the last 10/20/30 days.

> ⚠️ **Read this first.** No bot can guarantee profit — trading is risky and you
> can lose money. This system is built *risk-first*: its priority is to protect
> your capital and lock in gains, because that's what keeps an account alive.
> **Start on a DEMO account.** Only switch to LIVE once you've watched it behave
> for a while and you understand it. You are responsible for any money you trade.

---

## 📦 What's inside

```
TradingBot/
├── config/config.yaml      # ALL settings: symbols, timeframes, risk, strategy
├── .env.example            # secrets template (copy to .env)
├── run_bot.py              # ► start the autonomous bot
├── run_backtest.py         # ► backtest the strategy on recent history
├── dashboard/app.py        # ► live web dashboard (Streamlit)
└── src/
    ├── config.py           # config + .env loader
    ├── broker.py           # MT5 connection, data, orders, positions
    ├── indicators.py       # EMA, RSI, ATR, ADX, MACD, Bollinger (pure pandas)
    ├── strategy.py         # "Confluence Scalper" signal generation
    ├── risk.py             # lot sizing, daily limits, drawdown kill-switch
    ├── trade_manager.py    # break-even + trailing SL + partial TP
    ├── news.py             # economic calendar + headlines filter
    ├── store.py            # SQLite state (bot writes, dashboard reads)
    ├── backtest.py         # historical replay of the live strategy
    └── engine.py           # the 24/7 orchestration loop
```

---

## 🧠 How it trades (the strategy)

**Confluence Scalper.** Scalping fast timeframes is noisy, so the edge comes
from *only trading high-quality setups*. Every cycle the bot scores up to **6
independent conditions** on each entry timeframe:

1. EMA(9) vs EMA(21) — momentum direction
2. Price vs EMA(21) — short-term bias
3. MACD histogram sign — momentum
4. MACD histogram rising/falling — momentum acceleration
5. RSI above/below 50 — momentum
6. Higher-timeframe (M15) EMA(50) trend — the big-picture bias

A trade is only considered when:
- the aligned **confluence score ≥ 4 / 6**, **and**
- **ADX ≥ 20** (skip choppy, directionless markets), **and**
- **ATR%** is high enough (skip dead markets), **and**
- RSI is **not** at a chase-y extreme (no buying blow-off tops), **and**
- it does **not** fight a clear M15 trend, **and**
- **at least 2 of the 3 entry timeframes (1m/3m/5m) agree** on direction.

This multi-filter approach throws away most signals on purpose — quality over
quantity.

### 🛡️ Profit protection (your key requirement)

Once in a trade, three escalating protections kick in (all measured in **R**,
where 1R = your initial stop distance):

| Stage | Trigger | Action |
|------|---------|--------|
| **Break-even** | +1.0R | Move stop to entry (+ small buffer). Trade can no longer lose. |
| **Partial TP** | +1.5R | Bank 50% of the position as real profit; let the rest run. |
| **Trailing stop** | +1.5R | Stop chases the best price at 2×ATR behind it. |

So if you're in profit and the market **suddenly reverses**, you exit in profit
(or break-even) instead of giving the money back or hitting your original stop.
Stops only ever move in the safer direction — never looser.

### 💰 Position sizing & safety rails

- **Lot size** is computed so the loss-at-stop ≈ **2% of balance** per trade
  (your chosen risk level — change `risk.risk_per_trade` in the config).
- **Daily loss limit** (−6%): stops opening trades for the rest of the day.
- **Drawdown kill-switch** (−20% from equity peak): halts the bot entirely.
- **Max open trades**, **one position per symbol**, **spread guard**, and a
  **news blackout window** around high-impact events.

All tunable in `config/config.yaml`.

---

## 🚀 Setup (Windows — required for live trading)

> The official `MetaTrader5` Python library **only runs on Windows**. To trade
> 24/7, run this on a **Windows VPS** (e.g. a cheap cloud Windows box) so it
> stays online when your PC is off.

**1. Install prerequisites**
- Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add to PATH").
- Install your broker's **MetaTrader 5 terminal** and log into your account
  once manually. In MT5: *Tools → Options → Expert Advisors → Allow algorithmic
  trading* (enable it).

**2. Get the code & install deps**
```bash
git clone <your-repo-url>
cd TradingBot
pip install -r requirements.txt
```

**3. Configure secrets**
```bash
copy .env.example .env      # (Linux/mac: cp .env.example .env)
```
Edit `.env` and fill in your MT5 `LOGIN`, `PASSWORD`, `SERVER`. Keep
`TRADING_MODE=DEMO` to start.

**4. Set your symbol names** ⚠️ *Most common mistake.*
Open `config/config.yaml` and make `symbols → name` match your broker's
**exact** Market Watch names. Gold might be `XAUUSD`, `XAUUSD.m`, or `GOLD`;
BTC might be `BTCUSD` or `BTCUSD.m`. Right-click the symbol in MT5 to check.

**5. Run the bot**
```bash
python run_bot.py
```
Leave the MT5 terminal open and logged in while the bot runs.

**6. Open the dashboard** (separate terminal)
```bash
streamlit run dashboard/app.py
```
Then open `http://localhost:8501` (or your VPS IP) in a browser / on your phone.

---

## 🧪 Backtesting

See how the strategy performed recently:

```bash
python run_backtest.py                    # last 30/20/10 days, all symbols/TFs
python run_backtest.py --days 60 30 10    # custom windows
```

Output (per symbol/timeframe) shows trades, win-rate, profit factor, return %,
max drawdown and total R for each window. Results also appear in the
dashboard's **Backtest** tab.

**No MT5 / on Linux or Mac?** Backtest from a CSV instead:
```bash
python run_backtest.py --csv data/XAUUSD_M5.csv --symbol XAUUSD --tf M5
```
CSV columns: `time,open,high,low,close,volume`.

> ⚠️ Backtests use *completed-bar* signals and conservative intrabar exit
> assumptions, but they still can't model every real-world fill, slippage spike,
> or news gap. Treat them as a sanity check, not a promise. Past performance
> does not predict future results.

---

## 🔁 Going live

When you're satisfied on DEMO:
1. Set `TRADING_MODE=LIVE` in `.env`.
2. Double-check `risk_per_trade` and the daily/drawdown limits.
3. Start small. Restart the bot.

To keep it running 24/7 on a Windows VPS, use **Task Scheduler** (run at logon,
restart on failure) or a tool like **NSSM** to run `run_bot.py` as a service.

---

## ⚙️ Key config knobs (`config/config.yaml`)

> **Risk values are PERCENTAGES.** Under `risk:` (and a symbol's
> `risk_per_trade`), write `2` for 2%, `20` for 20%, `0` for off — **not**
> `0.02`. Position size, the daily loss limit, the profit target and the
> drawdown kill-switch all read as percent.

| Setting | Meaning |
|--------|---------|
| `risk.risk_per_trade` | Percent of balance risked per trade (`2` = 2%). |
| `risk.daily_loss_limit` | Stop trading for the day at this % loss (`20` = 20%). |
| `risk.max_drawdown_stop` | Kill-switch: halt bot at this % drawdown from peak (`10` = 10%). |
| `exits.sl_atr_mult` / `tp_atr_mult` | Stop / target distance in ATRs. |
| `exits.breakeven_at_r` / `trail_start_r` | When profit protection kicks in. |
| `strategy.min_confluence_score` | How many of 6 signals must agree (raise = fewer, higher-quality trades). |
| `news.block_minutes_before/after` | News blackout window size. |

---

## 🩹 Troubleshooting

- **"Symbol X not found"** → your `config.yaml` name doesn't match the broker's.
- **"MT5 initialize() failed"** → MT5 terminal not running/logged in, or
  algo-trading disabled in MT5 options.
- **"MetaTrader5 package not installed"** → you're not on Windows, or
  `pip install MetaTrader5` failed.
- **No trades happening** → that's often correct: the filters are strict. Lower
  `min_confluence_score` or `adx_min` to trade more (at lower quality).

---

*Built as a complete, risk-first scaffold. Tune the parameters to your broker,
your symbols and your risk appetite — and always validate on DEMO first.*
