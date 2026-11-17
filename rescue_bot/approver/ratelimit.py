"""Límite de acciones de escritura por app, calculado desde la auditoría.

Una ejecución en curso se reserva en memoria: solo una a la vez por app. La
reserva se libera después de escribir la entrada `executed`, que es la que cuenta.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from rescue_bot.approver.audit import AuditLog
from rescue_bot.config import Limits

HOUR_S = 3600


class RateLimiter:
    def __init__(self, audit: AuditLog, limits: Limits, clock: Callable[[], float] = time.time):
        self._audit = audit
        self._limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def check(self, app: str) -> str | None:
        """Motivo del bloqueo, o None si hay hueco."""
        with self._lock:
            return self._blocked(app)

    def reserve(self, app: str) -> str | None:
        """Como `check`, y si hay hueco lo reserva de forma atómica."""
        with self._lock:
            reason = self._blocked(app)
            if reason is None:
                self._inflight.add(app)
            return reason

    def release(self, app: str) -> None:
        with self._lock:
            self._inflight.discard(app)

    def _blocked(self, app: str) -> str | None:
        if app in self._inflight:
            return "ya hay una ejecución en curso en esa app"
        now = self._clock()
        epochs = self._audit.executed_epochs(app)
        if epochs and now - max(epochs) < self._limits.min_interval_s:
            return f"mínimo {self._limits.min_interval_s} s entre acciones"
        if sum(1 for t in epochs if now - t < HOUR_S) >= self._limits.max_per_hour:
            return f"límite de {self._limits.max_per_hour} acciones por hora"
        return None
