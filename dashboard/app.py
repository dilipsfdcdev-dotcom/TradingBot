"""
Streamlit dashboard — single-page live view of the bot.

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
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load_config  # noqa: E402
from src.store import Store  # noqa: E402

st.set_page_config(page_title="Trading Bot", page_icon="🤖", layout="wide")
st.markdown("<style>.block-container{padding-top:1.6rem;padding-bottom:1rem;}</style>",
            unsafe_allow_html=True)

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
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
        if secs < 60:
            return f"{secs:.0f}s ago"
        if secs < 3600:
            return f"{secs / 60:.0f}m ago"
        return f"{secs / 3600:.1f}h ago"
    except (ValueError, TypeError):
        return "—"


status = store.get_status()
acc = status.get("account", {})
mode = status.get("mode", "?")
state = status.get("state", "?")
heartbeat = status.get("heartbeat", "")
can_trade = status.get("can_trade", {})
need = cfg["strategy"]["min_confluence_score"]
alive = ago(heartbeat).endswith("s ago")

_DIR = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "NONE": "⚪ none", "NODATA": "⚫ no data"}
_STATE = {
    "TRADE": "✅ Trade placed", "holding": "📌 In trade", "no_signal": "🔍 Scanning",
    "no_agreement": "🔍 Scanning (mixed)", "spread_wide": "⚠️ Spread too wide",
    "news_blocked": "📰 Paused for news", "market_closed": "🌙 Market closed",
    "no_data": "❌ No data", "paused": "⏸️ Paused", "error": "❗ Error",
    "reversed": "🔄 Reversed",
}

# the 6 confluence signals -> short labels for the compact grid
_SIGNAL_SHORT = {
    "ema_cross": "EMA ✕",
    "price_vs_emaslow": "Px / EMA",
    "macd": "MACD",
    "macd_momentum": "MACD ▲▼",
    "rsi_mid": "RSI 50",
    "htf_trend": "M15 trend",
}


def parse_votes(reasons):
    """Split a signal's reasons into (votes, notes).

    votes = [(name, +1/-1)] for the 6 confluence checks; notes = gate/veto
    messages (ADX too low, RSI overbought veto, etc.).
    """
    votes, notes = [], []
    for r in reasons or []:
        name, sep, val = r.rpartition(":")
        if sep and val.strip() in ("+1", "-1"):
            votes.append((name, 1 if val.strip() == "+1" else -1))
        else:
            notes.append(r)
    return votes, notes

# ═══════════════════ HEADER ═══════════════════
c1, c2 = st.columns([3, 1])
with c1:
    badge = "🟢 LIVE" if mode == "LIVE" else "🔵 DEMO"
    dot = "🟢 online" if alive else "🔴 offline"
    st.title("🤖 Autonomous Trading Bot")
    st.markdown(f"**{badge}**  &nbsp;|&nbsp;  bot: **{dot}** (❤ {ago(heartbeat)})"
                f"  &nbsp;|&nbsp;  engine: `{state}`")
with c2:
    auto = st.toggle("Auto-refresh (5s)", value=True)
    if st.button("🔄 Refresh now", use_container_width=True):
        st.rerun()

if not can_trade.get("ok", True):
    st.warning(f"⛔ **Not opening new trades:** {can_trade.get('reason', '')}")

# ═══════════════════ KPIs ═══════════════════
eqdf = fetch_df("SELECT * FROM equity ORDER BY ts DESC LIMIT 5000")
day_start = None
if not eqdf.empty:
    eqdf = eqdf.iloc[::-1]
    eqdf["ts"] = pd.to_datetime(eqdf["ts"])
    today = pd.Timestamp.now(tz="UTC").normalize()
    today_rows = eqdf[eqdf["ts"] >= today]
    if not today_rows.empty:
        day_start = today_rows["equity"].iloc[0]

equity = acc.get("equity", 0)
day_pnl = (equity - day_start) if day_start else 0.0
day_pct = (day_pnl / day_start * 100) if day_start else 0.0
cur = acc.get("currency", "")

k = st.columns(5)
k[0].metric("Balance", f"{acc.get('balance', 0):,.0f} {cur}")
k[1].metric("Equity", f"{equity:,.0f}")
k[2].metric("Open P/L", f"{acc.get('profit', 0):,.2f}")
k[3].metric("Today's P/L", f"{day_pnl:,.0f}", delta=f"{day_pct:+.2f}%")
k[4].metric("Free Margin", f"{acc.get('free_margin', 0):,.0f}")

st.divider()

# ═══════════════════ LIVE SIGNALS (hero) ═══════════════════
_n_entry = len(cfg["timeframes"]["entry"])
_agree = f"  ·  {min(2, _n_entry)} timeframes must agree" if _n_entry >= 2 else ""
st.subheader(f"🔎 Live Signals  ·  need ≥ {need}/6{_agree}")

for sym in [s["name"] for s in cfg["symbols"] if s["enabled"]]:
    sstate = status.get(f"state_{sym}", {})
    sigs = status.get(f"signals_{sym}", {})
    label = _STATE.get(sstate.get("state", ""), sstate.get("state", "?"))
    detail = sstate.get("detail", "")

    with st.container(border=True):
        head = st.columns([2, 4])
        head[0].markdown(f"### {sym}")
        head[1].markdown(f"### {label}")
        if detail:
            head[1].caption(detail)

        if not sigs:
            st.info("waiting for first scan…")

        for tf, info in sigs.items():
            score = info.get("score", 0)
            d = info.get("direction", "NONE")
            snap = info.get("snapshot", {})
            votes, notes = parse_votes(info.get("reasons", []))
            n_buy = sum(1 for _, v in votes if v > 0)
            n_sell = len(votes) - n_buy

            ok = "✅" if score >= need else "⏳"
            st.markdown(
                f"**{tf}** &nbsp; {_DIR.get(d, d)} &nbsp; {ok} **{score}/6** "
                f"&nbsp;·&nbsp; 🟢 {n_buy} buy / 🔴 {n_sell} sell &nbsp;·&nbsp; "
                f"RSI {snap.get('rsi', '–')} · ADX {snap.get('adx', '–')} · "
                f"trend {snap.get('htf_bias', '–')}")

            if votes:
                grid = st.columns(len(votes))
                for cell, (name, v) in zip(grid, votes):
                    color = "green" if v > 0 else "red"
                    arrow = "🟢" if v > 0 else "🔴"
                    cell.markdown(f"{arrow} **{_SIGNAL_SHORT.get(name, name)}**")
                    cell.markdown(f":{color}[{'BUY' if v > 0 else 'SELL'}]")
            for note in notes:
                st.caption(f"⚠️ {note}")

st.divider()

# ═══════════════════ OPEN POSITIONS ═══════════════════
st.subheader("💼 Open Positions")
snap = fetch_df("SELECT * FROM positions_snapshot ORDER BY ts DESC LIMIT 1")
positions = []
if not snap.empty:
    try:
        positions = json.loads(snap.iloc[0]["data"])
    except (json.JSONDecodeError, TypeError):
        positions = []
if positions:
    pdf = pd.DataFrame(positions)
    st.dataframe(
        pdf[["symbol", "type", "volume", "price_open", "price_current",
             "sl", "tp", "profit"]],
        use_container_width=True, hide_index=True,
        column_config={"profit": st.column_config.NumberColumn("profit", format="%.2f")},
    )
    st.metric("Total open P/L", f"{sum(p.get('profit', 0) for p in positions):,.2f}")
else:
    st.success("Flat — no open positions.")

st.divider()

# ═══════════════════ TRADES + NEWS ═══════════════════
b_left, b_right = st.columns(2)
with b_left:
    st.subheader("🧾 Recent Activity")
    ev = fetch_df("SELECT * FROM trade_events ORDER BY ts DESC LIMIT 20")
    if not ev.empty:
        ev["time"] = pd.to_datetime(ev["ts"]).dt.strftime("%m-%d %H:%M:%S")
        st.dataframe(ev[["time", "event", "symbol", "direction", "volume",
                         "price", "profit", "detail"]],
                     use_container_width=True, hide_index=True, height=280)
    else:
        st.caption("No trades yet.")

with b_right:
    st.subheader("📰 News & Economic Events")
    up = status.get("upcoming_news", [])
    if up:
        st.markdown("**⚠️ Upcoming high-impact events**")
        for e in up[:6]:
            t = e.get("time", "")[11:16]
            st.markdown(f"- `{t} UTC`  **{e.get('country', '')}** — {e.get('title', '')}")
    else:
        st.caption("No high-impact events scheduled soon.")

    news = fetch_df("SELECT * FROM news ORDER BY ts DESC LIMIT 1")
    items = []
    if not news.empty:
        try:
            items = json.loads(news.iloc[0]["data"])
        except (json.JSONDecodeError, TypeError):
            items = []
    if items:
        with st.expander(f"📑 Latest headlines ({len(items)})", expanded=False):
            for it in items[:15]:
                title, url = it.get("title", ""), it.get("url", "")
                st.markdown(f"- [{title}]({url})" if url else f"- {title}")

# ═══════════════════ BACKTEST (collapsed) ═══════════════════
bt = status.get("backtest", {})
if bt:
    with st.expander("🧪 Backtest results", expanded=False):
        for key, report in bt.items():
            st.markdown(f"**{key}**")
            rows = []
            for days, s in report.items():
                if s.get("trades", 0) == 0:
                    rows.append({"window": f"{days}d", "trades": 0})
                    continue
                rows.append({"window": f"{days}d", "trades": s["trades"],
                             "win%": s["win_rate"], "PF": s["profit_factor"],
                             "return%": s.get("return_pct"),
                             "maxDD%": s.get("max_drawdown_pct"),
                             "total_R": s["total_R"]})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

if auto:
    time.sleep(5)
    st.rerun()
