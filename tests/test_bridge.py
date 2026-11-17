from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from bridge.broker import Position, SymbolSpec, TradeResult
from bridge.config import Settings
from bridge.engine import Engine, SignalError, compute_trailing_sl
from bridge.server import create_app
from bridge.store import SignalStore

SPEC = SymbolSpec("XAUUSD", 3, 0.001, 0.0, 0.01, 0.01, 200.0)
S = Settings(dedup_window_sec=0)


class FakeBroker:
    def __init__(self, bid=4000.0, ask=4000.2):
        self.bid, self.ask = bid, ask
        self.pos: list[Position] = []
        self.next_ticket = 1
        self.fail_close = False
        self.log: list[str] = []
        self.eq, self.realized = 10_000.0, 0.0

    def connect(self): pass
    def shutdown(self): pass
    def is_connected(self): return True
    def symbol_spec(self, symbol): return SPEC
    def tick(self, symbol): return self.bid, self.ask
    def balance(self): return 10_000.0
    def account_summary(self):
        return {'login': 1, 'server': 'Fake', 'currency': 'USD', 'balance': 10_000.0, 'equity': self.eq,
                'margin_free': self.eq, 'floating': 0.0, 'demo': True}
    def closed_trades(self, days=30):
        return [{'position': 9, 'symbol': 'XAUUSD', 'side': 'buy', 'volume': 0.01, 'open_time': 1, 'close_time': 2,
                 'open_price': 4000.0, 'close_price': 4002.0, 'profit': 2.0, 'reason': 'bridge'}]
    def equity(self): return self.eq
    def day_stats(self): return self.realized, sum(1 for x in self.log if x.startswith('open'))
    def loss_per_lot(self, symbol, side, distance): return distance * 100
    def positions(self, symbol=None): return [p for p in self.pos if symbol in (None, p.symbol)]

    def open(self, symbol, side, volume, sl_distance, tp_distance, comment=""):
        price = self.ask if side == "buy" else self.bid
        sign = 1 if side == "buy" else -1
        p = Position(self.next_ticket, symbol, side, volume, price,
                     price - sign * sl_distance if sl_distance else 0.0, 0.0)
        self.next_ticket += 1
        self.pos.append(p)
        self.log.append(f"open {side} {volume}")
        return TradeResult(True, 10009, "done", p.ticket, price)

    def close(self, pos):
        if self.fail_close:
            return TradeResult(False, 10018, "market closed")
        self.pos.remove(pos)
        self.log.append(f"close {pos.side}")
        return TradeResult(True, 10009, "done", pos.ticket, self.bid)

    def modify_sl(self, pos, sl):
        pos.sl = sl
        return TradeResult(True, 10009, "done")


# ---- signals --------------------------------------------------------------
def test_buy_opens_with_stop_loss():
    b = FakeBroker()
    r = Engine(b, S).handle_signal("buy", "XAUUSD")
    assert r["status"] == "executed"
    assert [(p.side, p.volume, p.sl) for p in b.pos] == [("buy", 0.01, pytest.approx(3995.2))]


def test_opposite_signal_closes_then_reverses():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy")
    r = e.handle_signal("sell", "OANDA:XAUUSD")
    assert r["closed"] == [1]
    assert b.log == ["open buy 0.01", "close buy", "open sell 0.01"]
    assert [p.side for p in b.pos] == ["sell"]


def test_same_direction_signal_is_ignored():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy")
    assert e.handle_signal("long")["status"] == "ignored"
    assert len(b.pos) == 1


def test_failed_close_blocks_new_open():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy")
    b.fail_close = True
    assert e.handle_signal("sell")["status"] == "error"
    assert [p.side for p in b.pos] == ["buy"]


def test_wide_spread_closes_opposite_but_does_not_open():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy")
    b.ask = b.bid + 2.0
    r = e.handle_signal("sell")
    assert r["status"] == "ignored" and r["closed"] == [1]
    assert b.pos == []


def test_close_and_duplicates_and_bad_input():
    b = FakeBroker()
    e = Engine(b, replace(S, dedup_window_sec=5, max_open_positions=2, max_positions_per_symbol=2))
    e.handle_signal("buy")
    assert e.handle_signal("buy")["reason"] == "duplicate signal"
    assert e.handle_signal("close")["closed"] == [1]
    with pytest.raises(SignalError):
        e.handle_signal("hold")
    with pytest.raises(SignalError):
        e.handle_signal("buy", "EURUSD")


def test_risk_percent_sizing_and_lot_cap():
    b = FakeBroker()
    # 1% of 10,000 = 100 risk; 5.0 SL costs 500 per lot -> 0.20 lots
    Engine(b, replace(S, risk_percent=1.0, max_lot=5)).handle_signal("buy")
    assert b.pos[0].volume == pytest.approx(0.20)
    b2 = FakeBroker()
    Engine(b2, replace(S, max_lot=0.5)).handle_signal("buy", lot=3)
    assert b2.pos[0].volume == pytest.approx(0.5)


def test_position_and_daily_trade_limits():
    b = FakeBroker()
    e = Engine(b, replace(S, max_open_positions=2, max_positions_per_symbol=2))
    assert [e.handle_signal("buy")["status"] for _ in range(3)] == ["executed", "executed", "ignored"]
    assert len(b.pos) == 2
    assert e.handle_signal("sell")["closed"] == [1, 2]      # reversal closes both, opens one
    assert [p.side for p in b.pos] == ["sell"]

    b2 = FakeBroker()
    e2 = Engine(b2, replace(S, max_trades_per_day=1))
    e2.handle_signal("buy")
    assert e2.handle_signal("sell")["reason"] == "MAX_TRADES_PER_DAY reached"


def test_daily_loss_kill_switch_closes_and_blocks():
    b = FakeBroker()
    e = Engine(b, replace(S, max_daily_loss_percent=50))
    e.handle_signal("buy")
    b.eq = 5_100.0
    e.trail_once()
    assert len(b.pos) == 1                                   # 49% down: still trading
    b.eq = 4_900.0
    e._last_risk_check = 0
    e.trail_once()
    assert b.pos == []                                       # 51% down: flattened
    b.eq = 10_000.0
    assert "daily loss" in e.handle_signal("buy")["reason"]  # stays off for the day
    assert e.handle_signal("close")["status"] == "executed"


def test_reentry_after_stop_out_while_signal_still_stands():
    b = FakeBroker()
    events = []
    e = Engine(b, replace(S, reentry_max=2, reentry_buffer=0.5, reentry_cooldown_sec=0, trail_distance=0),
               on_event=lambda *a: events.append(a))
    e.handle_signal("buy")                      # long from 4000.2
    b.bid, b.ask = 4006.0, 4006.2
    e.trail_once()                              # best price while in the trade: 4006.0
    b.pos.clear()                               # stopped out by the broker
    b.bid, b.ask = 4004.0, 4004.2
    e.trail_once()
    assert b.pos == []                          # pulled back: stay flat
    b.bid, b.ask = 4006.2, 4006.4
    e.trail_once()
    assert b.pos == []                          # not yet beyond 4006.0 + 0.5
    b.bid, b.ask = 4006.4, 4006.6
    e.trail_once()
    assert [p.side for p in b.pos] == ["buy"] and events[0][0] == "re-entry buy"

    b.pos.clear(); e.trail_once()               # stopped again
    b.bid, b.ask = 4010.0, 4010.2; e.trail_once()
    assert len(b.pos) == 1                      # second re-entry
    b.pos.clear(); e.trail_once()
    b.bid, b.ask = 4020.0, 4020.2; e.trail_once()
    assert b.pos == []                          # limit of 2 reached: wait for the next signal

    e.handle_signal("sell")                     # opposite signal flips direction and resets the count
    assert [p.side for p in b.pos] == ["sell"] and e.bias["XAUUSD"]["reentries"] == 0
    b.pos.clear(); e.trail_once()
    e.handle_signal("close")                    # close signal: stay flat, no re-entry
    b.bid, b.ask = 3990.0, 3990.2; e.trail_once()
    assert b.pos == [] and e.bias == {}


def test_reentry_is_off_by_default_and_respects_kill_switch():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy"); b.pos.clear()
    b.bid, b.ask = 4050.0, 4050.2
    e.trail_once(); e.trail_once()
    assert b.pos == []

    b2 = FakeBroker()
    e2 = Engine(b2, replace(S, reentry_max=2, reentry_cooldown_sec=0, max_daily_loss_percent=50))
    e2.handle_signal("buy"); e2.trail_once(); b2.pos.clear()
    b2.eq = 4_000.0                             # down 60% on the day
    b2.bid, b2.ask = 4050.0, 4050.2
    e2.trail_once(); e2.trail_once()
    assert b2.pos == []


# ---- trailing -------------------------------------------------------------
def buy_pos(sl=3995.0):
    return Position(1, "XAUUSD", "buy", 0.01, 4000.0, sl, 0.0)


def sell_pos(sl=4005.0):
    return Position(2, "XAUUSD", "sell", 0.01, 4000.0, sl, 0.0)


def test_trailing_waits_for_trigger():
    assert compute_trailing_sl(buy_pos(), 4001.9, 4002.1, S, SPEC) is None
    assert compute_trailing_sl(sell_pos(), 3997.9, 3998.1, S, SPEC) is None


def test_trailing_follows_price():
    assert compute_trailing_sl(buy_pos(), 4003.0, 4003.2, S, SPEC) == pytest.approx(4001.5)
    assert compute_trailing_sl(sell_pos(), 3996.8, 3997.0, S, SPEC) == pytest.approx(3998.5)


def test_trailing_never_loosens_and_respects_step():
    assert compute_trailing_sl(buy_pos(sl=4001.5), 4002.5, 4002.7, S, SPEC) is None   # price fell back
    assert compute_trailing_sl(buy_pos(sl=4001.5), 4003.1, 4003.3, S, SPEC) is None   # +0.1 < step 0.2
    assert compute_trailing_sl(buy_pos(sl=4001.5), 4003.2, 4003.4, S, SPEC) == pytest.approx(4001.7)
    assert compute_trailing_sl(sell_pos(sl=3998.5), 3997.5, 3997.7, S, SPEC) is None


def test_breakeven_before_trailing():
    s = replace(S, breakeven_at=1.0, breakeven_offset=0.1)
    assert compute_trailing_sl(buy_pos(), 4001.0, 4001.2, s, SPEC) == pytest.approx(4000.1)
    assert compute_trailing_sl(sell_pos(), 3998.8, 3999.0, s, SPEC) == pytest.approx(3999.9)


def test_trail_once_moves_stop():
    b = FakeBroker()
    e = Engine(b, S)
    e.handle_signal("buy")           # opens at 4000.2
    b.bid, b.ask = 4003.2, 4003.4
    assert e.trail_once() == 1
    assert b.pos[0].sl == pytest.approx(4001.7)
    assert e.trail_once() == 0


# ---- webhook --------------------------------------------------------------
@pytest.fixture
def client():
    b = FakeBroker()
    app = create_app(replace(S, trail_poll_ms=3_600_000, dashboard_password='pw'), b, SignalStore(':memory:'))
    with TestClient(app) as c:
        c.broker = b
        yield c


def test_webhook_executes_text_plain_json(client):
    r = client.post("/webhook", content='{"secret":"test-secret","action":"buy","symbol":"XAUUSD"}',
                    headers={"content-type": "text/plain"})
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert len(client.broker.pos) == 1


def test_webhook_rejects_bad_requests(client):
    assert client.post("/webhook", json={"secret": "nope", "action": "buy"}).status_code == 401
    assert client.post("/webhook", json={"action": "buy"}).status_code == 401
    assert client.post("/webhook", content="buy now").status_code == 400
    assert client.post("/webhook", json={"secret": "test-secret", "action": "dance"}).status_code == 400
    assert client.broker.pos == []


def test_webhook_ignores_stale_signal(client):
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/webhook", json={"secret": "test-secret", "action": "buy", "time": old})
    assert r.json()["status"] == "ignored"
    assert client.broker.pos == []


def test_dashboard_requires_password_from_remote(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/dashboard").status_code == 401
    assert client.get("/api/state", auth=("x", "wrong")).status_code == 401
    assert client.get("/dashboard", auth=("x", "pw")).status_code == 200


def test_dashboard_state_reports_signals_and_pnl(client):
    client.post("/webhook", json={"secret": "test-secret", "action": "buy", "symbol": "XAUUSD"})
    client.post("/webhook", json={"secret": "nope", "action": "sell"})
    s = client.get("/api/state", auth=("x", "pw")).json()
    assert [(x["action"], x["status"]) for x in s["signals"]] == [("sell", "rejected"), ("buy", "executed")]
    assert "nope" not in str(s) and "test-secret" not in str(s)
    assert len(s["positions"]) == 1 and s["account"]["currency"] == "USD"
    assert s["stats"]["trades"] == 1 and s["stats"]["pnl"] == 2.0 and s["stats"]["win_rate"] == 100.0
