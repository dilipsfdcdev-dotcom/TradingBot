# TradingView → MT5 bridge

Executes TradingView indicator alerts as trades in MetaTrader 5, within milliseconds of the alert arriving.

```
TradingView alert ──webhook──▶ bridge (this repo, on the MT5 machine) ──▶ MT5 terminal ──▶ broker
                                   └─ trailing-stop loop, checks price every 200 ms
```

What it does on each signal:

- **buy** – closes every open sell from this bridge, then opens a buy with a stop-loss.
- **sell** – closes every open buy from this bridge, then opens a sell with a stop-loss.
- **close** / **close_buy** / **close_sell** – closes positions without opening anything.
- A repeat signal in the direction you already hold adds a position until `MAX_POSITIONS_PER_SYMBOL` is reached.
- If a trade is stopped out while the last signal still stands, the bridge re-enters in that direction when price
  makes a new high (buy) or low (sell), up to `REENTRY_MAX` times per signal. A `close` signal cancels this.
- If the day's loss reaches `MAX_DAILY_LOSS_PERCENT`, all bridge positions are closed and trading stops until the next UTC day.
- Once a trade is in profit by `TRAIL_START`, the stop follows price at `TRAIL_DISTANCE` and never moves back.

The bridge only touches positions it opened itself (matched by `MAGIC`). Manual trades are left alone.

## Setup

Requirements: Windows, the MT5 terminal installed and logged in, Python 3.10+.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then set WEBHOOK_SECRET and review the other values
```

In MT5, turn on **Algo Trading** (toolbar button) and keep the terminal running.

Allow TradingView to reach the bridge (run once, in an Administrator PowerShell):

```powershell
New-NetFirewallRule -DisplayName "TradingView bridge" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

Start the bridge:

```powershell
.\start.bat
```

Check it from a browser: `http://<server-ip>/health` should show `{"ok":true,"mt5_connected":true}`.

## TradingView alerts

Webhooks need a paid TradingView plan with two-factor authentication enabled.

Create one alert per signal on your indicator (one for Buy, one for Sell):

1. **Condition**: your indicator → its Buy (or Sell) signal.
2. **Trigger**: *Once Per Bar Close* avoids acting on signals that repaint mid-bar. Use *Once Per Bar* only if
   the indicator does not repaint and you want the entry before the bar closes.
3. **Notifications → Webhook URL**: `http://<server-ip>/webhook`
4. **Message** (Buy alert):

   ```json
   {"secret":"YOUR_WEBHOOK_SECRET","action":"buy","symbol":"{{ticker}}","time":"{{timenow}}"}
   ```

   Sell alert: the same with `"action":"sell"`.

Optional fields: `"lot": 0.05` overrides the position size for that signal (capped by `MAX_LOT`).
`time` lets the bridge discard alerts that arrive more than `MAX_SIGNAL_AGE_SEC` late.

If the TradingView ticker differs from the MT5 symbol name, add it to `SYMBOL_MAP` in `.env`.

## Settings

All settings live in `.env` and are documented in [.env.example](.env.example). Restart the bridge after editing.
Distances are in price units: for gold, `SL_DISTANCE=5.0` is a $5 move in the gold price.

| Setting | Meaning |
| --- | --- |
| `LOT` / `RISK_PERCENT` | Fixed lot size, or % of equity risked at the stop-loss |
| `MAX_OPEN_POSITIONS`, `MAX_POSITIONS_PER_SYMBOL` | Open trades allowed in total and per symbol |
| `MAX_TRADES_PER_DAY`, `MAX_DAILY_LOSS_PERCENT` | Daily trade cap and daily-loss kill switch (0 = off) |
| `SL_DISTANCE`, `TP_DISTANCE` | Initial stop-loss and take-profit distance (0 = none) |
| `TRAIL_START`, `TRAIL_DISTANCE`, `TRAIL_STEP` | When trailing begins, how far behind price, minimum move |
| `BREAKEVEN_AT`, `BREAKEVEN_OFFSET` | Optional move to breakeven before trailing |
| `MAX_SPREAD` | Skip new entries when the spread is wider than this |
| `DRY_RUN` | `true` validates orders with the broker without sending them |

## Dashboard

Open `http://localhost/dashboard` on the machine running the bridge. It shows every alert received and what the
bridge did with it, open positions, closed trades and P&L, refreshed every 2 seconds. It is local-only unless
`DASHBOARD_PASSWORD` is set, in which case other devices can open `http://<server-ip>/dashboard` with that password.

## Testing

```powershell
.venv\Scripts\python.exe -m pytest -q                         # unit tests, no MT5 needed
.venv\Scripts\python.exe scripts\send_test_signal.py buy      # sends a signal to the running bridge
```

`send_test_signal.py` places a real order on the logged-in account unless `DRY_RUN=true`.

## Operating notes

- Stop-losses are held by the broker, so open trades stay protected if this machine or the bridge goes down.
  Trailing pauses until the bridge is back.
- If closing the opposite position fails, the bridge does not open the new one and logs an error.
- Logs are written to `logs/bridge.log`.
- To start the bridge automatically at logon, add `start.bat` to Task Scheduler.
- Test on a demo account before pointing this at real money.
