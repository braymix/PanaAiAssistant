"""Scheduler VRAM-aware (§A.6). Sostituisce il semaforo unico `max_concurrency`.

Il vincolo reale non e' "3 task": e' la VRAM. Ammissione PESATA: ogni task locale
prenota `concurrency_weight` MB (~ est_vram_mb del tier); si ammette finche'
`prenotato + peso <= budget`. I task 'frontier' (subscription) NON consumano VRAM
-> cap separato piccolo (`sub_concurrency`) per non floodare l'API.

Fallback: senza GPU misurabile si torna al cap semplice (comportamento attuale),
cosi' nulla si rompe. Testabile senza GPU: budget/gpu iniettati dal costruttore.

Include l'aggancio di PAUSA della coda (missione ciclo di vita, §B.2): la pausa
e' controllata QUI, prima di `acquire`; i task gia' ammessi proseguono.
"""

from __future__ import annotations

import asyncio


class VramScheduler:
    def __init__(self, *, budget_mb: int, headroom_mb: int, simple_cap: int,
                 sub_cap: int, gpu_present: bool) -> None:
        self._budget = max(0, budget_mb - headroom_mb)
        self._headroom = headroom_mb
        self._gpu_present = gpu_present
        self._simple_cap = max(1, simple_cap)
        self._sub_cap = max(1, sub_cap)
        # stato d'occupazione
        self._reserved_mb = 0      # VRAM prenotata dai task locali (modalita' GPU)
        self._local_active = 0     # task locali in volo (per il cap semplice)
        self._sub_active = 0       # task subscription in volo
        self._cond = asyncio.Condition()
        self._paused = False

    # --- costruzione dal profilo hardware --------------------------------------
    @classmethod
    def from_profile(cls, profile, settings) -> "VramScheduler":
        gpu = profile.gpu_name is not None and profile.vram_total_mb > 0
        return cls(
            budget_mb=profile.vram_free_mb if gpu else 0,
            headroom_mb=settings.vram_headroom_mb,
            simple_cap=settings.max_local_concurrency,
            sub_cap=settings.sub_concurrency,
            gpu_present=gpu,
        )

    # --- ammissione ------------------------------------------------------------
    def _can_admit(self, weight: int, is_local: bool) -> bool:
        if not is_local:
            return self._sub_active < self._sub_cap
        if not self._gpu_present:
            return self._local_active < self._simple_cap
        # VRAM pesata: ammetti sempre almeno UNO (evita il deadlock se un heavy
        # pesa piu' del budget), altrimenti finche' prenotato + peso <= budget.
        if self._reserved_mb == 0:
            return True
        return self._reserved_mb + weight <= self._budget

    async def acquire(self, weight: int, *, is_local: bool = True) -> None:
        async with self._cond:
            await self._cond.wait_for(
                lambda: (not self._paused) and self._can_admit(weight, is_local))
            if not is_local:
                self._sub_active += 1
            else:
                self._local_active += 1
                if self._gpu_present:
                    self._reserved_mb += max(0, weight)

    async def release(self, weight: int, *, is_local: bool = True) -> None:
        async with self._cond:
            if not is_local:
                self._sub_active = max(0, self._sub_active - 1)
            else:
                self._local_active = max(0, self._local_active - 1)
                if self._gpu_present:
                    self._reserved_mb = max(0, self._reserved_mb - max(0, weight))
            self._cond.notify_all()

    # --- pausa/ripresa della coda (§B.2) ---------------------------------------
    async def pause(self) -> None:
        async with self._cond:
            self._paused = True
            self._cond.notify_all()

    async def resume(self) -> None:
        async with self._cond:
            self._paused = False
            self._cond.notify_all()  # drena gli attese

    def set_paused(self, value: bool) -> None:
        """Setter sincrono (per il ripristino dello stato all'avvio)."""
        self._paused = bool(value)

    @property
    def paused(self) -> bool:
        return self._paused

    # --- osservabilita' (stats §A.8) -------------------------------------------
    @property
    def reserved_mb(self) -> int:
        return self._reserved_mb

    @property
    def budget_mb(self) -> int:
        return self._budget

    @property
    def gpu_present(self) -> bool:
        return self._gpu_present

    @property
    def local_active(self) -> int:
        return self._local_active

    @property
    def sub_active(self) -> int:
        return self._sub_active
