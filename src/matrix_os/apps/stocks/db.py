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


class StockCache:
    """SQLite-based cache for stock data."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or get_db_path()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
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
        try:
            import zoneinfo

            eastern = zoneinfo.ZoneInfo("America/New_York")
            now = datetime.now(eastern)
            return now.strftime("%Y-%m-%d")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d")

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
