"""Fixtures compartidas por tests/test_scenarios.py ."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from rescue_bot import config
from rescue_bot.enroll import render_actions_conf
from rescue_bot.presets import Action

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "config"
SHELL = ROOT / "vm" / "rescue-shell"


@dataclass
class Vm:
    """VM simulada: rescue-shell real, catálogo generado por enroll, `logger` falso (syslog)."""

    env: dict
    conf: Path
    marker_dir: Path
    syslog: Path

    def markers(self) -> list[str]:
        return sorted(p.name for p in self.marker_dir.iterdir())

    def log(self) -> str:
        return self.syslog.read_text() if self.syslog.exists() else ""


@pytest.fixture
def example_app():
    return config.load_apps(EXAMPLES)["example"]


@pytest.fixture
def vm(tmp_path, example_app) -> Vm:
    """Cada verbo del catálogo de la app de ejemplo, con el argv sustituido por `touch <verbo>`.

    Los verbos y modos son los reales (render_actions_conf del catálogo de la app);
    solo el comando cambia, para poder demostrar que no se ejecutó nada.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    actions = [
        Action(a.verb, a.mode, ("/usr/bin/touch", str(markers / a.verb)))
        for a in example_app.actions
    ]
    conf = tmp_path / "actions.conf"
    conf.write_text(render_actions_conf(actions))
    conf.chmod(0o644)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    syslog = tmp_path / "syslog"
    logger = bin_dir / "logger"
    logger.write_text(f'#!/bin/sh\necho "$@" >> {syslog}\n')
    logger.chmod(0o755)
    if not shutil.which("timeout"):  # macOS: shim solo de tests
        shim = bin_dir / "timeout"
        shim.write_text('#!/bin/sh\n[ "$1" = -k ] && shift 2\nshift\nexec "$@"\n')
        shim.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "RESCUE_SHELL_TESTING": "1"}
    return Vm(env, conf, markers, syslog)
