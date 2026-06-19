#!/usr/bin/env python3
"""
Diagnose why a symbol isn't trading.

    python scripts/diagnose.py

For every symbol in config.yaml it connects to MT5 and reports:
  - whether the symbol EXISTS at your broker (and the exact name)
  - whether it's visible / selectable in Market Watch
  - current bid/ask/spread
  - whether the bot considers the market OPEN right now
  - whether recent bars are available on each entry timeframe

This is the fastest way to find out why (e.g.) XAUUSD is silent while
BTCUSD trades.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config            # noqa: E402
from src.engine import market_open            # noqa: E402


def main() -> None:
    cfg = load_config()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 not installed (Windows only). Can't diagnose live.")
        return

    kwargs = {}
    if cfg.mt5_path:
        kwargs["path"] = cfg.mt5_path
    if cfg.mt5_login:
        kwargs.update(login=cfg.mt5_login, password=cfg.mt5_password,
                      server=cfg.mt5_server)
    if not mt5.initialize(**kwargs):
        print("MT5 initialize() FAILED:", mt5.last_error())
        print("→ Is the MT5 terminal open and logged in? Algo-trading enabled?")
        return

    acc = mt5.account_info()
    print(f"Connected: account {acc.login} @ {acc.server} "
          f"| balance {acc.balance:.2f} {acc.currency}\n")

    now = datetime.now(timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M} ({now:%A})\n")

    # Show ALL symbols whose name looks like gold, to help find the right one
    gold_like = [s.name for s in mt5.symbols_get() if "XAU" in s.name.upper()
                 or "GOLD" in s.name.upper()]
    print(f"Gold-like symbols available at your broker: {gold_like or 'NONE FOUND'}\n")

    tf_map = {"M1": mt5.TIMEFRAME_M1, "M3": mt5.TIMEFRAME_M3,
              "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}

    for sym in cfg["symbols"]:
        name = sym["name"]
        print("=" * 60)
        print(f"SYMBOL: {name}  (enabled={sym['enabled']}, session={sym.get('session')})")
        info = mt5.symbol_info(name)
        if info is None:
            print(f"  ❌ NOT FOUND at broker. Fix the name in config.yaml.")
            if gold_like and "XAU" in name.upper():
                print(f"     Try one of: {gold_like}")
            continue
        if not info.visible:
            ok = mt5.symbol_select(name, True)
            print(f"  ⚠️  not in Market Watch — auto-select {'OK' if ok else 'FAILED'}")
        tick = mt5.symbol_info_tick(name)
        if tick and info.point:
            spread_pts = (tick.ask - tick.bid) / info.point
            print(f"  ✅ found | bid={tick.bid} ask={tick.ask} "
                  f"spread={spread_pts:.0f} pts (max allowed {sym.get('max_spread')})")
            if spread_pts > sym.get("max_spread", 1e9):
                print(f"     ⚠️ spread EXCEEDS max_spread → trades skipped")
        is_open = market_open(sym.get("session", "24/7"), now)
        print(f"  market_open()? {'YES' if is_open else 'NO  ← bot will skip it now'}")
        print(f"  broker trade_mode={info.trade_mode} "
              f"(0=disabled, 4=full). volume_min={info.volume_min}")
        for tfn, tf in tf_map.items():
            rates = mt5.copy_rates_from_pos(name, tf, 0, 5)
            n = 0 if rates is None else len(rates)
            print(f"    {tfn}: {n} bars available")

    mt5.shutdown()
    print("\nDone. If XAUUSD shows NOT FOUND, copy the exact name from the list "
          "above into config/config.yaml.")


if __name__ == "__main__":
    main()
