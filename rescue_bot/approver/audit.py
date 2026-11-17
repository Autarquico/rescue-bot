"""Registro de auditoría: JSON lines de solo añadir, con fsync por escritura."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def write(
        self,
        event: str,
        *,
        app: str | None = None,
        verb: str | None = None,
        proposal: str | None = None,
        user: str | None = None,
        detail: object = None,
    ) -> None:
        now = self._clock()
        entry = {
            "ts": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
            "epoch": now,
            "event": event,
            "app": app,
            "verb": verb,
            "proposal": proposal,
            "user": user,
            "detail": detail,
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        with self._lock:
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def executed_epochs(self, app: str) -> list[float]:
        """Instantes de las ejecuciones (con éxito o no) de `app`."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        epochs = []
        for raw in lines:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("event") == "executed" and e.get("app") == app:
                epochs.append(float(e["epoch"]))
        return epochs
