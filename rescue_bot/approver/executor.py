"""Ejecución de un verbo rw con la clave de escritura, seguida de la comprobación de salud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rescue_bot import health, ssh
from rescue_bot.config import App


@dataclass(frozen=True)
class Outcome:
    code: int
    output: str
    health_ok: bool | None
    health_detail: str


class Executor:
    def __init__(
        self,
        key: Path,
        known_hosts: Path,
        run: Callable[..., ssh.Result] = ssh.run_verb,
        wait_healthy: Callable[[App], health.Health] = health.wait_healthy,
    ):
        self._key = key
        self._known_hosts = known_hosts
        self._run = run
        self._wait_healthy = wait_healthy

    def __call__(self, app: App, verb: str) -> Outcome:
        result = self._run(app, verb, self._key, self._known_hosts)
        h = self._wait_healthy(app)
        return Outcome(result.code, result.output, h.ok, h.detail)
