import asyncio
import logging
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
import aiosqlite

from backend.config import server_config

logger = logging.getLogger(__name__)


class RegistrationResult(Enum):
    SUCCESS = "SUCCESS"
    CAP_REACHED = "CAP_REACHED"
    IP_RATE_LIMITED = "IP_RATE_LIMITED"


class QuotaManager:
    """Manages tester quota, budget, and IP limits using a persistent aiosqlite connection."""

    def __init__(
        self,
        db_path: str = server_config.quota_db_path,
        enabled: bool | None = None,
    ):
        self.db_path = Path(db_path)
        # If enabled is explicitly supplied use it; otherwise fall back to config.
        self.enabled: bool = enabled if enabled is not None else server_config.quota_enabled
        self.max_total_registrations: int = server_config.max_testers
        self.max_registrations_per_ip: int = server_config.ip_registration_limit
        # Persistent connection — opened in initialize(), reused by every method.
        self._conn: aiosqlite.Connection | None = None
        # Serialises the check-then-insert in get_or_register so concurrent
        # callers cannot both pass the cap check before either inserts.
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.enabled:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        # WAL mode gives concurrent readers without blocking writers.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Retry for up to 5 s instead of failing immediately on lock contention.
        await self._conn.execute("PRAGMA busy_timeout=5000")

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS testers (
                id TEXT PRIMARY KEY,
                seconds_used REAL DEFAULT 0,
                ip_hash TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ip_registrations (
                ip_hash TEXT,
                registered_at TIMESTAMP
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                tester_id TEXT,
                rating TEXT,
                comment TEXT,
                submitted_at TIMESTAMP
            )
        """)
        await self._conn.commit()

    # Alias kept for backward-compatibility with the test fixture
    async def init_db(self) -> None:
        await self.initialize()

    async def get_or_register(self, tester_id: str, ip_hash: str) -> RegistrationResult:
        if not self.enabled or self._conn is None:
            return RegistrationResult.SUCCESS

        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id FROM testers WHERE id = ?", (tester_id,)
            )
            row = await cursor.fetchone()
            if row:
                return RegistrationResult.SUCCESS

            cursor = await self._conn.execute("SELECT COUNT(*) FROM testers")
            count_row = await cursor.fetchone()
            total_count = count_row[0] if count_row else 0

            if total_count >= self.max_total_registrations:
                return RegistrationResult.CAP_REACHED

            one_hour_ago = datetime.now(timezone.utc).timestamp() - 3600
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM ip_registrations WHERE ip_hash = ? AND registered_at >= ?",
                (ip_hash, one_hour_ago),
            )
            ip_count_row = await cursor.fetchone()
            ip_count = ip_count_row[0] if ip_count_row else 0

            if ip_count >= self.max_registrations_per_ip:
                return RegistrationResult.IP_RATE_LIMITED

            now = datetime.now(timezone.utc).timestamp()
            await self._conn.execute(
                "INSERT INTO testers (id, seconds_used, ip_hash, first_seen, last_seen) "
                "VALUES (?, 0, ?, ?, ?)",
                (tester_id, ip_hash, now, now),
            )
            await self._conn.execute(
                "INSERT INTO ip_registrations (ip_hash, registered_at) VALUES (?, ?)",
                (ip_hash, now),
            )
            await self._conn.commit()
            return RegistrationResult.SUCCESS

    async def get_seconds_used(self, tester_id: str) -> float:
        if not self.enabled or self._conn is None:
            return 0.0
        cursor = await self._conn.execute(
            "SELECT seconds_used FROM testers WHERE id = ?", (tester_id,)
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def record_usage(self, tester_id: str, seconds: float) -> None:
        if not self.enabled or self._conn is None or seconds <= 0:
            return
        now = datetime.now(timezone.utc).timestamp()
        await self._conn.execute(
            "UPDATE testers SET seconds_used = seconds_used + ?, last_seen = ? WHERE id = ?",
            (seconds, now, tester_id),
        )
        await self._conn.commit()

    async def get_status(self) -> dict:
        if not self.enabled or self._conn is None:
            return {
                "remaining_slots": self.max_total_registrations,
                "budget_seconds": server_config.tester_budget_sec,
            }
        cursor = await self._conn.execute("SELECT COUNT(*) FROM testers")
        row = await cursor.fetchone()
        count = row[0] if row else 0
        remaining = max(0, self.max_total_registrations - count)
        return {"remaining_slots": remaining, "budget_seconds": server_config.tester_budget_sec}

    async def save_feedback(self, tester_id: str, rating: str, comment: str | None = None) -> bool:
        if not self.enabled or self._conn is None:
            return True
        now = datetime.now(timezone.utc).timestamp()
        # Cap comment length at 1000 characters to prevent DB bloat
        safe_comment = (comment or "")[:1000]
        await self._conn.execute(
            "INSERT INTO feedback (tester_id, rating, comment, submitted_at) VALUES (?, ?, ?, ?)",
            (tester_id, str(rating), safe_comment, now),
        )
        await self._conn.commit()
        return True

    async def get_stats(self) -> dict:
        if not self.enabled or self._conn is None:
            return {"feedbacks": 0, "testers": 0}
        feedback_cursor = await self._conn.execute("SELECT COUNT(*) FROM feedback")
        feedback_row = await feedback_cursor.fetchone()
        tester_cursor = await self._conn.execute("SELECT COUNT(*) FROM testers")
        tester_row = await tester_cursor.fetchone()
        return {
            "feedbacks": feedback_row[0] if feedback_row else 0,
            "testers": tester_row[0] if tester_row else 0,
        }

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# Singleton instance
quota_manager = QuotaManager()
