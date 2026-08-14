"""Tests for the stocks app's daily API credit budget."""

import multiprocessing
import threading

import pytest

from matrix_os.apps.stocks import app as stocks_app
from matrix_os.apps.stocks.db import ApiBudget


@pytest.fixture
def budget_path(tmp_path):
    return str(tmp_path / "stocks.db")


def _spend_in_process(path, attempts, out):
    budget = ApiBudget(db_path=path, daily_limit=100)
    out.put(sum(1 for _ in range(attempts) if budget.try_spend(1)))


class FakeStocksApp(stocks_app.StocksApp):
    """StocksApp with the BaseApp/display machinery stripped off."""

    def __init__(self, path, limit=800, symbol="NVDA"):
        self._symbol = symbol
        self._api_key = "test-key"
        self._daily_credits = limit
        self._timezone = "America/New_York"
        self._exchange = "NYSE"
        self._open_time = 390
        self._is_fetching = False
        self._data_lock = threading.Lock()
        self._budget = ApiBudget(db_path=path, daily_limit=limit)

    def _get_budget(self):
        return self._budget


def test_budget_stops_at_daily_limit(budget_path):
    budget = ApiBudget(db_path=budget_path, daily_limit=10)
    assert sum(1 for _ in range(50) if budget.try_spend(1)) == 10
    assert budget.used() == 10
    assert budget.remaining() == 0


def test_budget_is_atomic_across_processes(budget_path):
    """The quota belongs to the API key, not the process -- apps run isolated."""
    queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=_spend_in_process, args=(budget_path, 80, queue))
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    granted = sum(queue.get() for _ in procs)
    for p in procs:
        p.join()

    assert granted == 100
    assert ApiBudget(db_path=budget_path, daily_limit=100).used() == 100


def test_reserve_protects_the_closing_snapshot(budget_path):
    app = FakeStocksApp(budget_path, limit=100)

    routine = 0
    while app._spend():
        routine += 1
    assert routine == 100 - stocks_app._CREDIT_RESERVE

    # Priority work still gets through after routine polling is cut off
    assert app._spend(reserve=0)


def test_trading_day_verdicts_are_cached(budget_path):
    """A settled date's trading status never changes, so it costs one credit once."""
    budget = ApiBudget(db_path=budget_path, daily_limit=800)
    assert budget.get_trading_day("2026-08-13") is None
    budget.set_trading_day("2026-08-13", True)
    assert budget.get_trading_day("2026-08-13") is True
    budget.set_trading_day("2026-08-15", False)
    assert budget.get_trading_day("2026-08-15") is False


def test_interval_stretches_then_stops_as_credits_deplete(budget_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(stocks_app.time, "time", lambda: clock[0])
    monkeypatch.setattr(stocks_app, "_market_close_ts", 390 * 60)

    app = FakeStocksApp(budget_path, limit=200)

    first = app._compute_update_interval()
    app._get_budget().try_spend(120)
    later = app._compute_update_interval()

    assert first is not None and later is not None
    assert later > first, "cadence should slow as credits deplete"

    # Once the spendable pool is gone, polling stops rather than failing calls
    app._get_budget().try_spend(200 - app._get_budget().used())
    assert app._compute_update_interval() is None


def test_api_key_is_redacted_from_logs(budget_path):
    """The web UI serves a log viewer -- a key logged once is a key leaked."""
    url = (
        "https://api.twelvedata.com/time_series?symbol=NVDA&interval=1day"
        "&timezone=America/New_York&apikey=53e1f986f6ec4e2e9e5e87a3d41a351e"
    )
    redacted = stocks_app._redact(url)

    assert "53e1f986f6ec4e2e9e5e87a3d41a351e" not in redacted
    assert "apikey=***" in redacted
    assert "symbol=NVDA" in redacted, "redaction should not destroy the useful parts"


def test_market_state_is_shared_not_rebilled(budget_path):
    """Market state belongs to the exchange, so symbols share one billed call."""
    nvda = FakeStocksApp(budget_path, symbol="NVDA")
    vti = FakeStocksApp(budget_path, symbol="VTI")
    vti._budget = nvda._budget  # same key, same ledger

    nvda._get_budget().set_market_state("NYSE", {"is_market_open": True})
    before = nvda._get_budget().used()

    assert nvda._get_market_state() == {"is_market_open": True}
    assert vti._get_market_state() == {"is_market_open": True}
    assert nvda._get_budget().used() == before, "second symbol was billed again"


def test_market_state_raises_when_budget_is_gone(budget_path):
    """Exhaustion must stop the call, not make it and fail."""
    app = FakeStocksApp(budget_path, limit=5)
    app._get_budget().try_spend(5)

    with pytest.raises(stocks_app._BudgetExhausted):
        app._get_market_state()


def test_overnight_wait_is_capped_on_the_local_clock(budget_path):
    """Waiting out the night must not cost a poll every 30 minutes."""
    app = FakeStocksApp(budget_path)
    seconds = app._seconds_until_expected_open()

    # Next weekday 9:30 ET is never more than a long weekend away
    assert 0 <= seconds <= 4 * 24 * 3600


def test_full_session_stays_within_budget(budget_path, monkeypatch):
    """Two symbols sharing one key must not overspend across a whole session."""
    clock = [0.0]
    monkeypatch.setattr(stocks_app.time, "time", lambda: clock[0])
    monkeypatch.setattr(stocks_app, "_market_close_ts", 390 * 60)

    limit = 800
    apps = [FakeStocksApp(budget_path, limit=limit, symbol=s) for s in ("NVDA", "VTI")]
    next_due = {a._symbol: 0.0 for a in apps}
    refreshes = {a._symbol: 0 for a in apps}

    for minute in range(391):
        clock[0] = minute * 60
        for app in apps:
            if clock[0] < next_due[app._symbol]:
                continue
            interval = app._compute_update_interval()
            if interval is None:
                next_due[app._symbol] = float("inf")
                continue
            next_due[app._symbol] = clock[0] + interval
            if app._spend():
                refreshes[app._symbol] += 1

    used = apps[0]._get_budget().used()
    assert used <= limit, "overspent the daily allowance"
    assert used <= limit - stocks_app._CREDIT_RESERVE, "routine polling ate the reserve"
    assert all(count > 0 for count in refreshes.values()), "a symbol was starved"
