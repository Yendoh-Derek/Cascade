import logging
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
import aiosqlite

from backend.config import server_config

logger = logging.getLogger(__name__)

class RegistrationResult(Enum):
    OK = "OK"
    CAP_REACHED = "CAP_REACHED"
    IP_RATE_LIMITED = "IP_RATE_LIMITED"


class QuotaManager:
    """Manages tester quota, budget, and IP limits using aiosqlite."""
    
    def __init__(self, db_path: str = server_config.quota_db_path):
        self.db_path = Path(db_path)
        self.enabled = server_config.quota_enabled
        self.max_testers = server_config.max_testers
        self.ip_limit = server_config.ip_registration_limit
        
    async def initialize(self):
        if not self.enabled:
            return
            
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as db:
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
            
    async def get_or_register(self, tester_id: str, ip_hash: str) -> RegistrationResult:
        if not self.enabled:
            return RegistrationResult.OK
            
        async with aiosqlite.connect(self.db_path) as db:
            # Check if exists
            cursor = await db.execute("SELECT id FROM testers WHERE id = ?", (tester_id,))
            row = await cursor.fetchone()
            if row:
                return RegistrationResult.OK
                
            # It's a new tester. Check global cap.
            cursor = await db.execute("SELECT COUNT(*) FROM testers")
            count_row = await cursor.fetchone()
            total_count = count_row[0] if count_row else 0
            
            if total_count >= self.max_testers:
                return RegistrationResult.CAP_REACHED
                
            # Check IP rate limit (registrations in the last hour)
            one_hour_ago = datetime.now(timezone.utc).timestamp() - 3600
            cursor = await db.execute(
                "SELECT COUNT(*) FROM ip_registrations WHERE ip_hash = ? AND registered_at >= ?", 
                (ip_hash, one_hour_ago)
            )
            ip_count_row = await cursor.fetchone()
            ip_count = ip_count_row[0] if ip_count_row else 0
            
            if ip_count >= self.ip_limit:
                return RegistrationResult.IP_RATE_LIMITED
                
            # Register
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
            return RegistrationResult.OK

    async def get_seconds_used(self, tester_id: str) -> float:
        if not self.enabled:
            return 0.0
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT seconds_used FROM testers WHERE id = ?", (tester_id,))
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0
            
    async def record_usage(self, tester_id: str, seconds: float):
        if not self.enabled or seconds <= 0:
            return
        now = datetime.now(timezone.utc).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE testers SET seconds_used = seconds_used + ?, last_seen = ? WHERE id = ?",
                (seconds, now, tester_id)
            )
            await db.commit()
            
    async def get_status(self) -> dict:
        if not self.enabled:
            return {"remaining_slots": self.max_testers, "budget_seconds": server_config.tester_budget_sec}
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM testers")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            remaining = max(0, self.max_testers - count)
            return {"remaining_slots": remaining, "budget_seconds": server_config.tester_budget_sec}
            
    async def save_feedback(self, tester_id: str, rating: str):
        if not self.enabled:
            return
        now = datetime.now(timezone.utc).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO feedback (tester_id, rating, submitted_at) VALUES (?, ?, ?)",
                (tester_id, rating, now)
            )
            await db.commit()

# Singleton instance
quota_manager = QuotaManager()
