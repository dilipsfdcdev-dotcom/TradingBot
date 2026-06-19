"""
Streamlit dashboard — single-screen live view of the bot.

Run with:   python -m streamlit run dashboard/app.py
It only READS the SQLite DB the bot writes to, so it is safe to start/stop
independently and never touches MT5.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load_config  # noqa: E402
from src.store import Store  # noqa: E402

st.set_page_config(page_title="Trading Bot", page_icon="🤖", layout="wide")

# tighter spacing so everything fits on one screen
st.markdown("""
<style>
.block-container {padding-top: 1.1rem; padding-bottom: 0.5rem; max-width: 100%;}
[data-testid="stMetricValue"] {font-size: 1.25rem;}
[data-testid="stMetricLabel"] {font-size: 0.72rem;}
[data-testid="stMetricDelta"] {font-size: 0.72rem;}
hr {margin: 0.35rem 0;}
h3 {margin-top: 0.2rem; margin-bottom: 0.2rem;}
div[data-testid="stVerticalBlockBorderWrapper"] {padding: 0.1rem 0;}
</style>
""", unsafe_allow_html=True)

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


def ago(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 60:
            return f"{secs:.0f}s ago"
        if secs < 3600:
            return f"{secs/60:.0f}m ago"
        return f"{secs/3600:.1f}h ago"
    except (ValueError, TypeError):
        return "—"


status = store.get_status()
acc = status.get("account", {})
mode = status.get("mode", "?")
state = status.get("state", "?")
heartbeat = status.get("heartbeat", "")
can_trade = status.get("can_trade", {})
need = cfg["strategy"]["min_confluence_score"]

_DIR = {"BUY": "🟢", "SELL": "🔴", "NONE": "⚪", "NODATA": "⚫"}
_STATE = {
    "TRADE": "✅ trade", "holding": "📌 holding", "no_signal": "🔍 scanning",
    "no_agreement": "🔍 scanning", "spread_wide": "⚠️ spread wide",
    "news_blocked": "📰 news block", "market_closed": "🌙 closed",
    "no_data": "❌ no data", "paused": "⏸️ paused", "error": "❗ error",
}

# ════════════════════════ HEADER ════════════════════════
h1, h2, h3 = st.columns([2.4, 1.4, 1])
with h1:
    badge = "🟢 LIVE" if mode == "LIVE" else "🔵 DEMO"
    dot = "🟢" if ago(heartbeat).endswith("s ago") else "🔴"
    st.markdown(f"### 🤖 Trading Bot &nbsp; {badge} &nbsp;·&nbsp; "
                f"{dot} `{state}` &nbsp;·&nbsp; ❤ {ago(heartbeat)}")
with h2:
    if not can_trade.get("ok", True):
        st.warning(f"⛔ {can_trade.get('reason','')}", icon="⚠️")
    else:
        st.success("✅ Trading enabled", icon="✅")
with h3:
    auto = st.toggle("Auto-refresh", value=True)
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

# ════════════════════════ KPI ROW ════════════════════════
day_start = None
eqdf = fetch_df("SELECT * FROM equity ORDER BY ts DESC LIMIT 5000")
if not eqdf.empty:
    eqdf = eqdf.iloc[::-1]
    eqdf["ts"] = pd.to_datetime(eqdf["ts"])
    today = pd.Timestamp.now(tz="UTC").normalize()
    today_rows = eqdf[eqdf["ts"] >= today]
    if not today_rows.empty:
        day_start = today_rows["equity"].iloc[0]

equity = acc.get("equity", 0)
day_pnl = (equity - day_start) if day_start else 0
day_pct = (day_pnl / day_start * 100) if day_start else 0
cur = acc.get("currency", "")

k = st.columns(6)
k[0].metric("Balance", f"{acc.get('balance', 0):,.0f}", help=cur)
k[1].metric("Equity", f"{equity:,.0f}")
k[2].metric("Open P/L", f"{acc.get('profit', 0):,.2f}",
            delta=f"{acc.get('profit', 0):,.2f}", delta_color="normal")
k[3].metric("Day P/L", f"{day_pnl:,.0f}", delta=f"{day_pct:+.2f}%")
k[4].metric("Used Margin", f"{acc.get('margin', 0):,.0f}")
k[5].metric("Free Margin", f"{acc.get('free_margin', 0):,.0f}")

st.divider()

# ════════════════════════ MAIN GRID ════════════════════════
left, right = st.columns([1.15, 1])

# ---- LEFT: live signals + open positions ----
with left:
    st.markdown(f"#### 🔎 Live signals · *need ≥ {need}/6 + 2 TFs agree*")
    sym_names = [s["name"] for s in cfg["symbols"] if s["enabled"]]
    for sym in sym_names:
        sstate = status.get(f"state_{sym}", {})
        sigs = status.get(f"signals_{sym}", {})
        label = _STATE.get(sstate.get("state", ""), sstate.get("state", "?"))
        detail = sstate.get("detail", "")
        with st.container(border=True):
            cols = st.columns([1.1] + [1] * max(len(sigs), 1))
            cols[0].markdown(f"**{sym}**<br><span style='font-size:0.8rem'>{label}</span>"
                             + (f"<br><span style='font-size:0.7rem;color:#888'>{detail}</span>"
                                if detail else ""), unsafe_allow_html=True)
            if not sigs:
                cols[1].caption("waiting...")
            for col, (tf, info) in zip(cols[1:], sigs.items()):
                score = info.get("score", 0)
                d = info.get("direction", "NONE")
                col.metric(f"{tf} {_DIR.get(d, '')}", f"{score}/6",
                           delta=f"{score - need:+d}",
                           delta_color="normal" if score >= need else "off")

    st.markdown("#### 💼 Open positions")
    snap = fetch_df("SELECT * FROM positions_snapshot ORDER BY ts DESC LIMIT 1")
    positions = []
    if not snap.empty:
        try:
            positions = json.loads(snap.iloc[0]["data"])
        except (json.JSONDecodeError, TypeError):
            positions = []
    if positions:
        pdf = pd.DataFrame(positions)
        show = pdf[["symbol", "type", "volume", "price_open",
                    "price_current", "sl", "tp", "profit"]].copy()
        st.dataframe(show, use_container_width=True, hide_index=True, height=160)
    else:
        st.caption("Flat — no open positions.")

# ---- RIGHT: equity curve + recent activity ----
with right:
    st.markdown("#### 📈 Equity")
    if not eqdf.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eqdf["ts"], y=eqdf["equity"], name="Equity",
                                 line=dict(color="#16c784", width=2),
                                 fill="tozeroy", fillcolor="rgba(22,199,132,0.08)"))
        fig.add_trace(go.Scatter(x=eqdf["ts"], y=eqdf["balance"], name="Balance",
                                 line=dict(color="#888", width=1, dash="dot")))
        fig.update_layout(height=230, margin=dict(l=0, r=0, t=5, b=0),
                          showlegend=False,
                          yaxis=dict(side="right"),
                          xaxis=dict(showgrid=False))
        fig.update_yaxes(range=[eqdf[["equity", "balance"]].min().min() * 0.999,
                                eqdf[["equity", "balance"]].max().max() * 1.001])
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.caption("Waiting for equity data...")

    st.markdown("#### 🧾 Recent activity")
    ev = fetch_df("SELECT * FROM trade_events ORDER BY ts DESC LIMIT 12")
    if not ev.empty:
        ev["t"] = pd.to_datetime(ev["ts"]).dt.strftime("%m-%d %H:%M")
        st.dataframe(ev[["t", "event", "symbol", "direction", "volume",
                         "price", "profit"]],
                     use_container_width=True, hide_index=True, height=200)
    else:
        st.caption("No trades yet.")

# ════════════════════════ SIDEBAR: news + backtest ════════════════════════
with st.sidebar:
    st.markdown("### ⚠️ Upcoming news")
    up = status.get("upcoming_news", [])
    if up:
        for e in up[:8]:
            t = e.get("time", "")[11:16]
            st.markdown(f"`{t}` **{e.get('country','')}** · {e.get('title','')[:40]}")
    else:
        st.caption("No high-impact events soon.")

    st.divider()
    st.markdown("### 📰 Headlines")
    news = fetch_df("SELECT * FROM news ORDER BY ts DESC LIMIT 1")
    if not news.empty:
        try:
            items = json.loads(news.iloc[0]["data"])
        except (json.JSONDecodeError, TypeError):
            items = []
        for it in items[:10]:
            title = it.get("title", "")[:70]
            url = it.get("url", "")
            st.markdown(f"- [{title}]({url})" if url else f"- {title}")
    else:
        st.caption("No headlines loaded.")

    st.divider()
    st.markdown("### 🧪 Backtest")
    bt = status.get("backtest", {})
    if bt:
        for key, report in bt.items():
            rows = [{"win": f"{report[d].get('win_rate','-')}",
                     "ret%": report[d].get("return_pct"),
                     "n": report[d].get("trades", 0), "w": f"{d}d"}
                    for d in report]
            st.caption(f"**{key}**")
            st.dataframe(pd.DataFrame(rows), hide_index=True,
                         use_container_width=True)
    else:
        st.caption("Run `python run_backtest.py`")

if auto:
    time.sleep(5)
    st.rerun()
