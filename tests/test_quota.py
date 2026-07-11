import os
import tempfile
import asyncio
import pytest
import pytest_asyncio

from backend.quota import QuotaManager, RegistrationResult


@pytest_asyncio.fixture
async def quota_manager():
    # Use a fresh temporary file for each test.
    fd, path = tempfile.mkstemp()
    os.close(fd)

    qm = QuotaManager(db_path=path, enabled=True)
    qm.max_total_registrations = 5
    qm.max_registrations_per_ip = 5

    await qm.initialize()
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
async def test_concurrent_registration_race_condition(quota_manager):
    """Two concurrent registrations against a cap of 1 — exactly one must succeed."""
    quota_manager.max_total_registrations = 1

    results = await asyncio.gather(
        quota_manager.get_or_register("tester_a", "ip_a"),
        quota_manager.get_or_register("tester_b", "ip_b"),
    )

    successes = results.count(RegistrationResult.SUCCESS)
    cap_hits = results.count(RegistrationResult.CAP_REACHED)
    assert successes == 1, f"Expected exactly 1 SUCCESS, got: {results}"
    assert cap_hits == 1, f"Expected exactly 1 CAP_REACHED, got: {results}"


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

    success = await quota_manager.save_feedback("tester_1", "5", "Great app!")
    assert success is True

    stats = await quota_manager.get_stats()
    assert stats["feedbacks"] == 1
