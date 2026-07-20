"""Scheduler VRAM-aware (§A.6). Testabile senza GPU: budget iniettato.

Verifica: ammissione pesata (N light ma 1 heavy), fallback cap semplice senza GPU,
cap separato per subscription, deadlock-avoidance (heavy > budget gira da solo),
pausa/ripresa della coda (§B.2).
"""

import asyncio

import pytest

from app.scheduler import VramScheduler


def _run(coro):
    return asyncio.run(coro)


async def _blocks(sched, weight, *, is_local=True, timeout=0.15):
    """True se l'acquire NON viene ammesso entro `timeout` (resta in attesa)."""
    try:
        await asyncio.wait_for(sched.acquire(weight, is_local=is_local), timeout)
        return False
    except asyncio.TimeoutError:
        return True


def test_admits_n_light_but_one_heavy():
    async def scenario():
        # budget 20000 (headroom 0). light=6000 -> 3 entrano; heavy=11000 -> 1.
        s = VramScheduler(budget_mb=20000, headroom_mb=0, simple_cap=99,
                          sub_cap=2, gpu_present=True)
        for _ in range(3):
            await asyncio.wait_for(s.acquire(6000), 1)
        assert s.reserved_mb == 18000
        assert await _blocks(s, 6000)          # il 4° light non entra

        s2 = VramScheduler(budget_mb=20000, headroom_mb=0, simple_cap=99,
                           sub_cap=2, gpu_present=True)
        await asyncio.wait_for(s2.acquire(11000), 1)   # 1 heavy entra
        assert await _blocks(s2, 11000)                # il 2° heavy no
    _run(scenario())


def test_release_frees_vram():
    async def scenario():
        s = VramScheduler(budget_mb=12000, headroom_mb=0, simple_cap=99,
                          sub_cap=2, gpu_present=True)
        await asyncio.wait_for(s.acquire(6000), 1)
        await asyncio.wait_for(s.acquire(6000), 1)   # 12000 pieno
        assert await _blocks(s, 6000)
        await s.release(6000)
        await asyncio.wait_for(s.acquire(6000), 1)   # ora rientra
        assert s.reserved_mb == 12000
    _run(scenario())


def test_no_gpu_falls_back_to_simple_cap():
    async def scenario():
        # senza GPU il peso e' ignorato: conta solo il cap semplice.
        s = VramScheduler(budget_mb=0, headroom_mb=1024, simple_cap=2,
                          sub_cap=2, gpu_present=False)
        await asyncio.wait_for(s.acquire(6000), 1)
        await asyncio.wait_for(s.acquire(6000), 1)   # cap=2 pieno
        assert await _blocks(s, 6000)                # il 3° attende
        assert s.reserved_mb == 0                    # niente contabilita' VRAM
        await s.release(6000)
        await asyncio.wait_for(s.acquire(6000), 1)
    _run(scenario())


def test_subscription_has_separate_cap():
    async def scenario():
        s = VramScheduler(budget_mb=0, headroom_mb=0, simple_cap=1,
                          sub_cap=2, gpu_present=True)
        await asyncio.wait_for(s.acquire(0, is_local=False), 1)
        await asyncio.wait_for(s.acquire(0, is_local=False), 1)  # sub_cap=2 pieno
        assert await _blocks(s, 0, is_local=False)
        await s.release(0, is_local=False)
        await asyncio.wait_for(s.acquire(0, is_local=False), 1)
    _run(scenario())


def test_heavy_bigger_than_budget_runs_alone():
    async def scenario():
        # deadlock-avoidance: se pesa piu' del budget, gira da solo (reserved==0).
        s = VramScheduler(budget_mb=8000, headroom_mb=0, simple_cap=99,
                          sub_cap=2, gpu_present=True)
        await asyncio.wait_for(s.acquire(11000), 1)   # ammesso da solo
        assert await _blocks(s, 6000)                 # ma nient'altro entra
    _run(scenario())


def test_pause_blocks_admission_resume_drains():
    async def scenario():
        s = VramScheduler(budget_mb=20000, headroom_mb=0, simple_cap=99,
                          sub_cap=2, gpu_present=True)
        await s.pause()
        assert s.paused
        assert await _blocks(s, 6000)     # in pausa non ammette
        await s.resume()
        assert not s.paused
        await asyncio.wait_for(s.acquire(6000), 1)   # ripresa: drena
    _run(scenario())


def test_from_profile_no_gpu():
    from app.config import Settings
    from app.hardware import HardwareProfile
    prof = HardwareProfile(gpu_name=None, vram_total_mb=0, vram_free_mb=0,
                           ram_mb=8000, cpu_cores=4, installed_models=[])
    s = VramScheduler.from_profile(prof, Settings())
    assert not s.gpu_present


def test_from_profile_with_gpu():
    from app.config import Settings
    from app.hardware import HardwareProfile
    prof = HardwareProfile(gpu_name="RTX", vram_total_mb=12288, vram_free_mb=11000,
                           ram_mb=32000, cpu_cores=16, installed_models=[])
    s = VramScheduler.from_profile(prof, Settings(vram_headroom_mb=1024))
    assert s.gpu_present and s.budget_mb == 11000 - 1024
