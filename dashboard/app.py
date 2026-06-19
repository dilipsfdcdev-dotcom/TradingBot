"""
Streamlit dashboard — live view of the bot.

Run with:   streamlit run dashboard/app.py
It only READS the SQLite DB the bot writes to, so it is safe to start/stop
independently and never touches MT5.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load_config  # noqa: E402
from src.store import Store  # noqa: E402

st.set_page_config(page_title="Trading Bot", page_icon="🤖", layout="wide")

cfg = load_config()


@st.cache_resource
def get_store():
    return Store(cfg.db_path)


store = get_store()


def fetch_df(sql, params=()):
    try:
        return pd.DataFrame(store.fetch_all(sql, params))
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


status = store.get_status()
acc = status.get("account", {})
mode = status.get("mode", "?")
state = status.get("state", "?")
heartbeat = status.get("heartbeat", "")
can_trade = status.get("can_trade", {})

# ── header ────────────────────────────────────────────────────────────────
left, right = st.columns([3, 1])
with left:
    st.title("🤖 Autonomous Trading Bot")
    badge = "🟢 LIVE" if mode == "LIVE" else "🔵 DEMO"
    st.markdown(f"**{badge}**  ·  state: `{state}`  ·  last heartbeat: `{heartbeat}`")
with right:
    auto = st.toggle("Auto-refresh", value=True)
    if st.button("🔄 Refresh now"):
        st.rerun()

if not can_trade.get("ok", True):
    st.warning(f"⛔ Not opening new trades — {can_trade.get('reason','')}")

# ── account metrics ─────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Balance", f"{acc.get('balance', 0):,.2f} {acc.get('currency','')}")
c2.metric("Equity", f"{acc.get('equity', 0):,.2f}")
c3.metric("Open P/L", f"{acc.get('profit', 0):,.2f}")
c4.metric("Used Margin", f"{acc.get('margin', 0):,.2f}")
c5.metric("Free Margin", f"{acc.get('free_margin', 0):,.2f}")

tabs = st.tabs(["📈 Overview", "💼 Positions", "🎯 Signals", "📰 News", "🧪 Backtest"])

# ── Overview ──────────────────────────────────────────────────────────────
with tabs[0]:
    eq = fetch_df("SELECT * FROM equity ORDER BY ts DESC LIMIT 2000")
    if not eq.empty:
        eq = eq.iloc[::-1]
        eq["ts"] = pd.to_datetime(eq["ts"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eq["ts"], y=eq["equity"], name="Equity",
                                 line=dict(color="#16c784", width=2)))
        fig.add_trace(go.Scatter(x=eq["ts"], y=eq["balance"], name="Balance",
                                 line=dict(color="#888", width=1, dash="dot")))
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10),
                          title="Equity curve")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Waiting for the bot to write equity data...")

    st.subheader("Recent trade events")
    ev = fetch_df("SELECT * FROM trade_events ORDER BY ts DESC LIMIT 50")
    if not ev.empty:
        st.dataframe(ev[["ts", "event", "symbol", "direction", "volume",
                         "price", "sl", "tp", "profit", "detail"]],
                     use_container_width=True, hide_index=True)
    else:
        st.caption("No trades yet.")

# ── Positions ─────────────────────────────────────────────────────────────
with tabs[1]:
    snap = fetch_df("SELECT * FROM positions_snapshot ORDER BY ts DESC LIMIT 1")
    if not snap.empty:
        try:
            positions = json.loads(snap.iloc[0]["data"])
        except (json.JSONDecodeError, TypeError):
            positions = []
        if positions:
            pdf = pd.DataFrame(positions)
            st.dataframe(pdf[["ticket", "symbol", "type", "volume", "price_open",
                              "price_current", "sl", "tp", "profit"]],
                         use_container_width=True, hide_index=True)
            total = sum(p.get("profit", 0) for p in positions)
            st.metric("Total open P/L", f"{total:,.2f}")
        else:
            st.success("Flat — no open positions.")
    else:
        st.caption("No position data yet.")

# ── Signals ───────────────────────────────────────────────────────────────
with tabs[2]:
    found = False
    for sym in cfg["symbols"]:
        key = f"signals_{sym['name']}"
        sigs = status.get(key)
        if not sigs:
            continue
        found = True
        state = status.get(f"state_{sym['name']}", {})
        state_label = state.get("state", "?")
        detail = state.get("detail", "")
        st.subheader(f"{sym['name']} — `{state_label}`"
                     + (f"  ·  {detail}" if detail else ""))
        cols = st.columns(len(sigs))
        for col, (tf, info) in zip(cols, sigs.items()):
            d = info.get("direction", "NONE")
            emoji = {"BUY": "🟢", "SELL": "🔴"}.get(d, "⚪")
            col.markdown(f"**{tf}**  {emoji} `{d}`")
            col.caption(f"score {info.get('score',0)}/6")
            snapshot = info.get("snapshot", {})
            if snapshot:
                col.json(snapshot, expanded=False)
    if not found:
        st.info("No signal snapshots yet — the bot writes these each cycle.")

# ── News ──────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("⚠️ Upcoming high-impact events")
    up = status.get("upcoming_news", [])
    if up:
        st.dataframe(pd.DataFrame(up), use_container_width=True, hide_index=True)
    else:
        st.caption("No upcoming high-impact events (or calendar not loaded).")

    st.subheader("📰 Latest headlines")
    news = fetch_df("SELECT * FROM news ORDER BY ts DESC LIMIT 1")
    if not news.empty:
        try:
            items = json.loads(news.iloc[0]["data"])
        except (json.JSONDecodeError, TypeError):
            items = []
        for it in items:
            title = it.get("title", "")
            url = it.get("url", "")
            src = it.get("source", "")
            st.markdown(f"- [{title}]({url}) — *{src}*" if url else f"- {title} — *{src}*")
    else:
        st.caption("No headlines loaded yet.")

# ── Backtest ──────────────────────────────────────────────────────────────
with tabs[4]:
    st.caption("Run `python run_backtest.py` to populate this.")
    bt = status.get("backtest", {})
    if bt:
        for key, report in bt.items():
            st.subheader(key)
            rows = []
            for days, s in report.items():
                if s.get("trades", 0) == 0:
                    rows.append({"window": f"{days}d", "trades": 0})
                    continue
                rows.append({
                    "window": f"{days}d", "trades": s["trades"],
                    "win%": s["win_rate"], "PF": s["profit_factor"],
                    "return%": s.get("return_pct"), "maxDD%": s.get("max_drawdown_pct"),
                    "total_R": s["total_R"],
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No backtest results saved yet.")

if auto:
    time.sleep(5)
    st.rerun()
