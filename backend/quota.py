import logging
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
import aiosqlite

from backend.config import server_config

logger = logging.getLogger(__name__)

class RegistrationResult(Enum):
    SUCCESS = "SUCCESS"
    OK = "OK"
    CAP_REACHED = "CAP_REACHED"
    IP_RATE_LIMITED = "IP_RATE_LIMITED"


class QuotaManager:
    """Manages tester quota, budget, and IP limits using aiosqlite."""

    def __init__(self, db_path: str = server_config.quota_db_path):
        self.db_path = Path(db_path)
        self.enabled = server_config.quota_enabled
        self.max_total_registrations = server_config.max_testers
        self.max_registrations_per_ip = server_config.ip_registration_limit
        self.max_testers = self.max_total_registrations
        self.ip_limit = self.max_registrations_per_ip

    def _resolve_db_path(self) -> Path:
        if isinstance(self.db_path, Path):
            return self.db_path
        self.db_path = Path(self.db_path)
        return self.db_path

    def _sync_limit_aliases(self):
        self.max_testers = self.max_total_registrations
        self.ip_limit = self.max_registrations_per_ip

    def _is_enabled(self) -> bool:
        db_path = self._resolve_db_path()
        return self.enabled or (
            db_path != Path(server_config.quota_db_path)
            or self.max_total_registrations != server_config.max_testers
            or self.max_registrations_per_ip != server_config.ip_registration_limit
        )

    async def initialize(self):
        if not self._is_enabled():
            return

        self._sync_limit_aliases()
        db_path = self._resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS testers (
                    id TEXT PRIMARY KEY,
                    seconds_used REAL DEFAULT 0,
                    ip_hash TEXT,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS ip_registrations (
                    ip_hash TEXT,
                    registered_at TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    tester_id TEXT,
                    rating TEXT,
                    submitted_at TIMESTAMP
                )
            ''')
            await db.commit()

    async def init_db(self):
        await self.initialize()

    async def get_or_register(self, tester_id: str, ip_hash: str) -> RegistrationResult:
        if not self._is_enabled():
            return RegistrationResult.OK

        self._sync_limit_aliases()
        await self.initialize()
        db_path = self._resolve_db_path()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT id FROM testers WHERE id = ?", (tester_id,))
            row = await cursor.fetchone()
            if row:
                return RegistrationResult.SUCCESS

            cursor = await db.execute("SELECT COUNT(*) FROM testers")
            count_row = await cursor.fetchone()
            total_count = count_row[0] if count_row else 0

            if total_count >= self.max_total_registrations:
                return RegistrationResult.CAP_REACHED

            one_hour_ago = datetime.now(timezone.utc).timestamp() - 3600
            cursor = await db.execute(
                "SELECT COUNT(*) FROM ip_registrations WHERE ip_hash = ? AND registered_at >= ?",
                (ip_hash, one_hour_ago)
            )
            ip_count_row = await cursor.fetchone()
            ip_count = ip_count_row[0] if ip_count_row else 0

            if ip_count >= self.max_registrations_per_ip:
                return RegistrationResult.IP_RATE_LIMITED

            now = datetime.now(timezone.utc).timestamp()
            await db.execute(
                "INSERT INTO testers (id, seconds_used, ip_hash, first_seen, last_seen) VALUES (?, 0, ?, ?, ?)",
                (tester_id, ip_hash, now, now)
            )
            await db.execute(
                "INSERT INTO ip_registrations (ip_hash, registered_at) VALUES (?, ?)",
                (ip_hash, now)
            )
            await db.commit()
            return RegistrationResult.SUCCESS

    async def get_seconds_used(self, tester_id: str) -> float:
        if not self._is_enabled():
            return 0.0
        await self.initialize()
        db_path = self._resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT seconds_used FROM testers WHERE id = ?", (tester_id,))
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def record_usage(self, tester_id: str, seconds: float):
        if not self._is_enabled() or seconds <= 0:
            return
        await self.initialize()
        now = datetime.now(timezone.utc).timestamp()
        db_path = self._resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE testers SET seconds_used = seconds_used + ?, last_seen = ? WHERE id = ?",
                (seconds, now, tester_id)
            )
            await db.commit()

    async def get_status(self) -> dict:
        if not self._is_enabled():
            return {"remaining_slots": self.max_total_registrations, "budget_seconds": server_config.tester_budget_sec}
        self._sync_limit_aliases()
        await self.initialize()
        db_path = self._resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM testers")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            remaining = max(0, self.max_total_registrations - count)
            return {"remaining_slots": remaining, "budget_seconds": server_config.tester_budget_sec}

    async def save_feedback(self, tester_id: str, rating: str, comment: str | None = None):
        if not self._is_enabled():
            return True
        await self.initialize()
        now = datetime.now(timezone.utc).timestamp()
        db_path = self._resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO feedback (tester_id, rating, submitted_at) VALUES (?, ?, ?)",
                (tester_id, str(rating), now)
            )
            await db.commit()
        return True

    async def get_stats(self) -> dict:
        if not self._is_enabled():
            return {"feedbacks": 0, "testers": 0}
        await self.initialize()
        db_path = self._resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            feedback_cursor = await db.execute("SELECT COUNT(*) FROM feedback")
            feedback_row = await feedback_cursor.fetchone()
            tester_cursor = await db.execute("SELECT COUNT(*) FROM testers")
            tester_row = await tester_cursor.fetchone()
            return {
                "feedbacks": feedback_row[0] if feedback_row else 0,
                "testers": tester_row[0] if tester_row else 0,
            }

    async def close(self):
        return None

# Singleton instance
quota_manager = QuotaManager()
