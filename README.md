# 🤖 Autonomous Scalping Fleet (Forex + Gold, 24/7)

A multi-agent trading system that connects to your **MT5** account and scalps
forex pairs and gold fully autonomously — no manual intervention needed:

- **One autonomous agent per symbol** (XAUUSD, EURUSD, GBPUSD, USDJPY out of the box)
- **Auto lot sizing** from your live equity: each trade risks a fixed % of the account
- **Stop-loss + take-profit on every order** (ATR-based, adapts to volatility)
- **Trailing stops** with break-even protection on a fast 2-second loop
- **Opens and closes trades** on its own (signal entries, momentum-flip exits, max-age exits)
- **Risk guards**: daily loss limit, max-drawdown kill-switch (auto-flattens everything), spread filter, exposure caps
- **News-aware**: pauses trading around high-impact events (free Forex Factory calendar feed)
- **Live web dashboard**: account info, open trades, equity curve, agent status, calendar, trade journal, emergency-stop button
- **Self-healing**: broker reconnect watchdog, weekend market-hours detection, optional Telegram alerts

> ⚠️ **Risk warning**: Trading forex/gold with leverage can lose money fast. This
> software comes with no profitability guarantee. **Run it on a demo account
> first** and only move to a live account with money you can afford to lose.

---

## How it works

```
main.py
 └─ Orchestrator
     ├─ Broker ──────────── MT5 via MetaApi cloud (real-time streaming state)
     ├─ SymbolAgent XAUUSD ┐
     ├─ SymbolAgent EURUSD │  scan → risk gates → size lot → order with SL/TP
     ├─ SymbolAgent GBPUSD │  manage → trail → exit
     ├─ SymbolAgent USDJPY ┘
     ├─ TrailingStopManager  (2s loop: break-even + ATR trailing)
     ├─ RiskManager          (daily loss halt, drawdown kill-switch, sizing)
     ├─ NewsService          (Forex Factory calendar → news blackout windows)
     └─ Dashboard            (FastAPI, http://localhost:8000)
```

**Strategy (per agent):** trade only with the 15-minute trend (EMA50/EMA200),
enter on a 5-minute EMA9/21 cross confirmed by RSI, skip dead/over-spread
markets, SL = 1.5×ATR, TP = 2.2×ATR, trail after +1×ATR, break-even at +0.6×ATR.
All parameters are per-symbol in `config.yaml`.

---

## Setup (10 minutes)

### 1. Connect your MT5 account

The bot talks to MT5 through [MetaApi](https://metaapi.cloud) — a cloud bridge
that works on any OS and keeps running even when your computer is off-friendly
hosting (free tier available):

1. Sign up at https://app.metaapi.cloud
2. Add your MT5 account (broker server, login, password)
3. Copy your **API token** and the **account id**

### 2. Configure

```bash
cp .env.example .env     # paste METAAPI_TOKEN and METAAPI_ACCOUNT_ID
```

Tune `config.yaml` if you want different symbols, risk %, or SL/TP multiples.
Defaults: 0.5% risk per trade, 4% max daily loss, 15% drawdown kill-switch.

### 3. Run

```bash
pip install -r requirements.txt
python main.py
```

Open **http://localhost:8000** for the live dashboard.

### Run 24/7 with Docker

```bash
docker compose up -d --build     # auto-restarts on crash/reboot
docker compose logs -f
```

Deploy the same compose file on any $5 VPS (Hetzner, DigitalOcean, Oracle
free tier) for true 24/7 operation.

---

## Dashboard

| Panel | Shows |
|---|---|
| Account | balance, equity, floating P/L, free margin, day-start & peak equity |
| Equity curve | live equity sparkline |
| Open positions | side, lots, entry, SL/TP, live P/L per trade |
| Agents | per-symbol status, trend, spread, ATR, why it's waiting |
| Calendar | next 48h of economic events with impact rating |
| Journal | every open/close/halt the fleet ever did (`logs/journal.jsonl`) |
| ⛔ Emergency stop | halts trading and closes all bot positions instantly |

## Safety model

| Guard | Default | What happens |
|---|---|---|
| Risk per trade | 0.5% of equity | lot size computed so SL hit ≈ 0.5% loss |
| Daily loss limit | 4% | no new trades until next UTC day |
| Max drawdown | 15% from peak | kill-switch: halt + close everything |
| Max open positions | 4 total, 1 per symbol | excess signals are skipped |
| Spread filter | per symbol | no entries when spread spikes |
| News blackout | ±15 min around High impact | no entries during the window |
| Magic number | 777001 | the bot only ever touches its own trades |

## Project layout

```
bot/
  agent.py         # autonomous per-symbol trader
  broker.py        # MT5 connection (MetaApi streaming)
  strategy.py      # scalping signal engine
  risk.py          # lot sizing + loss limits + kill-switch
  trailing.py      # break-even + ATR trailing stops
  news.py          # economic calendar guard
  orchestrator.py  # spawns agents + service loops
  journal.py       # JSONL audit trail
  notifier.py      # optional Telegram alerts
  config.py        # config.yaml + .env loader
dashboard/
  server.py        # FastAPI API
  static/index.html# live UI
config.yaml        # symbols, risk, strategy parameters
main.py            # entrypoint
```

## License

MIT — see [LICENSE](LICENSE).
