"""Ejecución de un verbo en una VM a través de su usuario `rescue`.

La VM decide qué hace el verbo (rescue-shell + actions.conf); aquí solo se envía
el nombre, validado, con la clave que corresponde a quien llama. El cliente SSH
no hereda configuración: sin ~/.ssh/config, sin agente, sin multiplexación y con
host keys verificadas contra el known_hosts de la configuración privada.
"""

from __future__ import annotations

import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from rescue_bot.config import App
from rescue_bot.presets import is_verb

TIMEOUT_S = 150
MAX_OUTPUT = 64 * 1024
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


@dataclass(frozen=True)
class Result:
    ok: bool
    code: int
    output: str


def ssh_argv(app: App, verb: str, key: Path, known_hosts: Path) -> list[str]:
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=10",
        "-T",
        f"{app.user}@{app.host}",
        verb,
    ]


def run_verb(app: App, verb: str, key: Path, known_hosts: Path, timeout: int = TIMEOUT_S) -> Result:
    if not is_verb(verb):
        return Result(False, 2, f"verbo inválido: {verb!r}")
    if not (USER_RE.fullmatch(app.user) and HOST_RE.fullmatch(app.host)):
        return Result(False, 2, f"destino inválido: {app.user}@{app.host}")
    proc = subprocess.Popen(
        ssh_argv(app, verb, key, known_hosts),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    chunks, size = [], 0
    try:
        stdout = proc.stdout
        if stdout is None:
            raise RuntimeError("ssh sin stdout")
        while chunk := stdout.read1(8192):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_OUTPUT:
                proc.kill()
                break
        code = proc.wait()
    finally:
        watchdog.cancel()
    truncated = size > MAX_OUTPUT or code == -signal.SIGKILL
    out = b"".join(chunks)[:MAX_OUTPUT].decode("utf-8", errors="replace")
    if truncated:
        out += "\n[salida truncada o tiempo agotado]"
    return Result(code == 0 and not truncated, code, out)
