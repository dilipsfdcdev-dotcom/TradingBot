#!/usr/bin/env python3
"""
Backtest runner — see how the strategy would have performed recently.

    python run_backtest.py                 # last 30/20/10 days, all symbols/TFs
    python run_backtest.py --days 60 30 10
    python run_backtest.py --csv data/XAUUSD_M5.csv --symbol XAUUSD --tf M5

By default it pulls history straight from MT5. If MT5 isn't available (e.g.
you're on Linux/Mac), point it at a CSV with columns:
    time,open,high,low,close,volume
Results are printed and also saved into the dashboard DB (Backtest tab).
"""
from __future__ import annotations

import argparse

import pandas as pd

from src.backtest import Backtester, windows_report
from src.config import load_config
from src.logger import get_logger, setup_logging
from src.store import Store


def _load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]]


def _print_report(symbol, tf, report):
    print(f"\n{'='*64}\n  {symbol}  {tf}\n{'='*64}")
    print(f"{'window':>8} | {'trades':>6} | {'win%':>5} | {'PF':>5} | "
          f"{'ret%':>7} | {'maxDD%':>7} | {'totR':>6}")
    print("-" * 64)
    for days, s in report.items():
        if s.get("trades", 0) == 0:
            print(f"{str(days)+'d':>8} | {'0':>6} |  (no trades / data)")
            continue
        print(f"{str(days)+'d':>8} | {s['trades']:>6} | {s['win_rate']:>5} | "
              f"{s['profit_factor']:>5} | {s.get('return_pct',0):>7} | "
              f"{s.get('max_drawdown_pct',0):>7} | {s['total_R']:>6}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", type=int, default=[30, 20, 10])
    ap.add_argument("--csv", help="CSV file instead of MT5")
    ap.add_argument("--symbol", help="symbol label (with --csv)")
    ap.add_argument("--tf", help="timeframe label (with --csv)")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir)
    log = get_logger("backtest")
    bt = Backtester(cfg)
    store = Store(cfg.db_path)
    risk = cfg["risk"]["risk_per_trade"]
    all_results = {}

    if args.csv:
        df = _load_csv(args.csv)
        symbol = args.symbol or "CSV"
        tf = args.tf or cfg["timeframes"]["entry"][0]
        rep = windows_report(df, bt, symbol, tf, risk, tuple(args.days))
        _print_report(symbol, tf, rep)
        all_results[f"{symbol}/{tf}"] = rep
    else:
        from src.broker import Broker
        broker = Broker(cfg)
        if not broker.connect():
            log.error("MT5 not available. Use --csv to backtest from a CSV file.")
            return
        max_days = max(args.days)
        for sym in cfg["symbols"]:
            if not sym["enabled"]:
                continue
            broker.ensure_symbol(sym["name"])
            for tf in cfg["timeframes"]["entry"]:
                df = broker.get_history(sym["name"], tf, max_days)
                if df is None or len(df) < 100:
                    log.warning("No data for %s %s", sym["name"], tf)
                    continue
                rsk = sym.get("risk_per_trade", risk)
                rep = windows_report(df, bt, sym["name"], tf, rsk, tuple(args.days))
                _print_report(sym["name"], tf, rep)
                all_results[f"{sym['name']}/{tf}"] = rep
        broker.shutdown()

    store.set_status("backtest", all_results)
    store.set_status("backtest_days", list(args.days))
    print("\nSaved results to dashboard (Backtest tab).")


if __name__ == "__main__":
    main()
