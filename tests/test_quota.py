import os
import tempfile
import pytest

from backend.quota import QuotaManager, RegistrationResult

@pytest.fixture
async def quota_manager():
    # Use an in-memory SQLite database for testing
    fd, path = tempfile.mkstemp()
    os.close(fd)
    
    # Overwrite the db_path to use the temporary file
    qm = QuotaManager()
    qm.db_path = path
    qm.max_total_registrations = 2 # Small number for testing
    qm.max_registrations_per_ip = 2
    
    await qm.init_db()
    yield qm
    
    await qm.close()
    os.remove(path)

@pytest.mark.asyncio
async def test_get_or_register_new_user(quota_manager):
    result = await quota_manager.get_or_register("tester_1", "ip_hash_1")
    assert result == RegistrationResult.SUCCESS

@pytest.mark.asyncio
async def test_get_or_register_existing_user(quota_manager):
    await quota_manager.get_or_register("tester_1", "ip_hash_1")
    result = await quota_manager.get_or_register("tester_1", "ip_hash_1")
    assert result == RegistrationResult.SUCCESS
    
@pytest.mark.asyncio
async def test_capacity_reached(quota_manager):
    quota_manager.max_total_registrations = 1
    
    res1 = await quota_manager.get_or_register("tester_1", "ip_hash_1")
    assert res1 == RegistrationResult.SUCCESS
    
    res2 = await quota_manager.get_or_register("tester_2", "ip_hash_2")
    assert res2 == RegistrationResult.CAP_REACHED

@pytest.mark.asyncio
async def test_ip_rate_limited(quota_manager):
    quota_manager.max_registrations_per_ip = 1
    
    res1 = await quota_manager.get_or_register("tester_1", "ip_hash_1")
    assert res1 == RegistrationResult.SUCCESS
    
    res2 = await quota_manager.get_or_register("tester_2", "ip_hash_1")
    assert res2 == RegistrationResult.IP_RATE_LIMITED

@pytest.mark.asyncio
async def test_record_usage(quota_manager):
    await quota_manager.get_or_register("tester_1", "ip_hash_1")
    
    await quota_manager.record_usage("tester_1", 30.5)
    used = await quota_manager.get_seconds_used("tester_1")
    assert used == 30.5
    
    await quota_manager.record_usage("tester_1", 10.0)
    used = await quota_manager.get_seconds_used("tester_1")
    assert used == 40.5

@pytest.mark.asyncio
async def test_save_feedback(quota_manager):
    await quota_manager.get_or_register("tester_1", "ip_hash_1")
    
    success = await quota_manager.save_feedback("tester_1", 5, "Great app!")
    assert success is True
    
    stats = await quota_manager.get_stats()
    assert stats["feedbacks"] == 1
