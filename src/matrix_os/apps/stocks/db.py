"""
Stock data cache using SQLite.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


def _get_data_dir() -> Path:
    """Get the data directory path, creating it if needed."""
    # Find project root (where data/ should be)
    # Go up from: src/matrix_os/apps/stocks/db.py -> project root
    current = Path(__file__).resolve()
    project_root = current.parent.parent.parent.parent.parent

    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_db_path() -> str:
    """Get the path to the stocks database."""
    return str(_get_data_dir() / "stocks.db")


def get_trading_date() -> str:
    """Today's date in US Eastern, YYYY-MM-DD."""
    try:
        import zoneinfo

        return datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


@dataclass
class StockData:
    """Cached stock data."""

    symbol: str
    current_price: float
    close_price: float
    difference: float
    percent: float
    inflection_pt: int
    graph_values: List[Tuple[int, int]]
    trading_day: str  # YYYY-MM-DD format
    updated: float


class ApiBudget:
    """Daily API credit ledger, shared across app processes.

    TwelveData meters requests against a per-key daily allowance. Each stock app
    runs in its own process, so a module-level counter would let every symbol
    independently believe it owns the whole quota. The ledger therefore lives in
    SQLite, where `BEGIN IMMEDIATE` gives us a cross-process compare-and-spend.

    The quota is keyed to the US Eastern date. Whatever wall clock the provider
    resets on, ET midnight is the one boundary guaranteed to fall outside market
    hours -- so a day's spending is never split mid-session, and a session never
    inherits yesterday's spending.
    """

    def __init__(self, db_path: Optional[str] = None, daily_limit: int = 800):
        self.db_path = db_path or get_db_path()
        self.daily_limit = daily_limit
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.isolation_level = None  # manage transactions explicitly
        return conn

    def _init_db(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS api_credits ("
                    "day TEXT PRIMARY KEY, used INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS trading_days ("
                    "day TEXT PRIMARY KEY, is_trading INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS api_symbols ("
                    "symbol TEXT PRIMARY KEY, last_seen REAL NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS market_state ("
                    "exchange TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched REAL NOT NULL)"
                )
                # Yesterday's ledger rows have no further use
                conn.execute("DELETE FROM api_credits WHERE day < date('now', '-7 day')")
        except Exception as e:
            log.warning("Failed to init API budget tables: %s", e)

    def try_spend(self, credits: int = 1, reserve: int = 0) -> bool:
        """Atomically claim `credits` if the day's allowance can cover them.

        `reserve` holds back part of the allowance so cheap, frequent polling
        cannot consume the credits needed for the end-of-day snapshot.
        Returns False when the request would exceed the budget -- callers must
        then skip the API call rather than making it anyway.
        """
        day = get_trading_date()
        try:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT used FROM api_credits WHERE day = ?", (day,)).fetchone()
                used = row[0] if row else 0

                if used + credits > max(0, self.daily_limit - reserve):
                    conn.execute("ROLLBACK")
                    return False

                conn.execute(
                    "INSERT INTO api_credits (day, used) VALUES (?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET used = used + ?",
                    (day, credits, credits),
                )
                conn.execute("COMMIT")
                return True
            finally:
                conn.close()
        except Exception as e:
            # Fail closed: an unusable ledger must not become unmetered spending
            log.warning("Credit ledger unavailable, refusing spend: %s", e)
            return False

    def used(self) -> int:
        """Credits spent so far today."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT used FROM api_credits WHERE day = ?", (get_trading_date(),)
                ).fetchone()
                return row[0] if row else 0
        except Exception as e:
            log.warning("Failed to read credit usage: %s", e)
            return self.daily_limit  # assume exhausted rather than overspend

    def remaining(self) -> int:
        """Credits still available today."""
        return max(0, self.daily_limit - self.used())

    def heartbeat(self, symbol: str) -> None:
        """Record that `symbol` is actively sharing the quota."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO api_symbols (symbol, last_seen) VALUES (?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET last_seen = ?",
                    (symbol, time.time(), time.time()),
                )
        except Exception as e:
            log.debug("Failed to record symbol heartbeat: %s", e)

    def active_symbols(self, max_age: float = 1800) -> int:
        """How many symbols are currently competing for the quota (minimum 1)."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM api_symbols WHERE last_seen > ?",
                    (time.time() - max_age,),
                ).fetchone()
                return max(1, row[0] if row else 1)
        except Exception as e:
            log.debug("Failed to count active symbols: %s", e)
            return 1

    def get_market_state(self, exchange: str, max_age: float = 300) -> Optional[dict]:
        """Recently fetched market state for an exchange, if still fresh.

        Market state is a property of the exchange, not the symbol, so every
        stock app can share one billed lookup instead of each paying its own.
        A cached `time_to_close` may lag by up to `max_age`, which only shifts
        scheduling by that much -- callers already pad their deadlines.
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT payload, fetched FROM market_state WHERE exchange = ?",
                    (exchange,),
                ).fetchone()
                if row and (time.time() - row[1]) < max_age:
                    return json.loads(row[0])
                return None
        except Exception as e:
            log.debug("Failed to read market state cache: %s", e)
            return None

    def set_market_state(self, exchange: str, payload: dict) -> None:
        """Share a freshly fetched market state with the other app processes."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO market_state (exchange, payload, fetched) "
                    "VALUES (?, ?, ?)",
                    (exchange, json.dumps(payload), time.time()),
                )
        except Exception as e:
            log.debug("Failed to cache market state: %s", e)

    def get_trading_day(self, day: str) -> Optional[bool]:
        """Cached trading-day verdict for a date, or None if never resolved."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT is_trading FROM trading_days WHERE day = ?", (day,)
                ).fetchone()
                return bool(row[0]) if row else None
        except Exception as e:
            log.debug("Failed to read trading day cache: %s", e)
            return None

    def set_trading_day(self, day: str, is_trading: bool) -> None:
        """Remember whether a date was a trading day.

        Only safe for settled dates. A "no data yet" answer for the current day
        means the session has not started, not that the market is shut, so the
        caller must not persist a False verdict for today.
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO trading_days (day, is_trading) VALUES (?, ?)",
                    (day, int(is_trading)),
                )
        except Exception as e:
            log.debug("Failed to cache trading day: %s", e)


class StockCache:
    """SQLite-based cache for stock data."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or get_db_path()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection, creating the db file if it doesn't exist."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            # Create table if not exists (don't drop - we want to keep data!)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stocks (
                    symbol TEXT PRIMARY KEY,
                    current_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    difference REAL NOT NULL,
                    percent REAL NOT NULL,
                    inflection_pt INTEGER NOT NULL,
                    graph_values TEXT NOT NULL,
                    trading_day TEXT NOT NULL,
                    updated REAL NOT NULL
                )
            """
            )
            conn.commit()

    def get(self, symbol: str) -> Optional[StockData]:
        """Get cached stock data."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT symbol, current_price, close_price, difference, percent,
                           inflection_pt, graph_values, trading_day, updated
                    FROM stocks WHERE symbol = ?
                    """,
                    (symbol,),
                )
                row = cursor.fetchone()

                if row is None:
                    return None

                return StockData(
                    symbol=row[0],
                    current_price=row[1],
                    close_price=row[2],
                    difference=row[3],
                    percent=row[4],
                    inflection_pt=row[5],
                    graph_values=json.loads(row[6]),
                    trading_day=row[7],
                    updated=row[8],
                )
        except Exception as e:
            log.warning("Failed to get cached stock data: %s", e)
            return None

    def set(self, data: StockData) -> None:
        """Cache stock data."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stocks
                    (symbol, current_price, close_price, difference, percent,
                     inflection_pt, graph_values, trading_day, updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.symbol,
                        data.current_price,
                        data.close_price,
                        data.difference,
                        data.percent,
                        data.inflection_pt,
                        json.dumps(data.graph_values),
                        data.trading_day,
                        data.updated,
                    ),
                )
                conn.commit()
                log.debug("Cached stock data for %s", data.symbol)
        except Exception as e:
            log.warning("Failed to cache stock data: %s", e)

    def is_stale(self, symbol: str, max_age: float = 300) -> bool:
        """Check if cached data is stale (older than max_age seconds or different day)."""
        data = self.get(symbol)
        if data is None:
            return True

        # Check if it's a different trading day
        today = self._get_trading_day()
        if data.trading_day != today:
            return True

        return time.time() - data.updated > max_age

    def _get_trading_day(self) -> str:
        """Get current trading day in YYYY-MM-DD format (US Eastern time)."""
        return get_trading_date()

    def clear(self, symbol: Optional[str] = None) -> None:
        """Clear cached data for a symbol or all symbols."""
        try:
            with self._get_connection() as conn:
                if symbol:
                    conn.execute("DELETE FROM stocks WHERE symbol = ?", (symbol,))
                else:
                    conn.execute("DELETE FROM stocks")
                conn.commit()
        except Exception as e:
            log.warning("Failed to clear stock cache: %s", e)
