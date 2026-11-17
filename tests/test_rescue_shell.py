import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SHELL = Path(__file__).resolve().parent.parent / "vm" / "rescue-shell"

CATALOGUE = """\
# verbo modo comando
status ro /bin/echo running
big ro /usr/bin/head -c 200000 /dev/zero
restart rw /usr/bin/touch {marker}
"""


@pytest.fixture
def env(tmp_path):
    marker = tmp_path / "marker"
    conf = tmp_path / "actions.conf"
    conf.write_text(CATALOGUE.format(marker=marker))
    conf.chmod(0o644)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if not shutil.which("timeout"):  # macOS: shim solo de tests
        shim = bin_dir / "timeout"
        shim.write_text('#!/bin/sh\nshift\nexec "$@"\n')
        shim.chmod(0o755)
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RESCUE_SHELL_TESTING": "1",
    }
    return base, conf, marker


def run(env, mode, verb, conf=None, testing=True):
    base, default_conf, _ = env
    environ = dict(base)
    if not testing:
        environ.pop("RESCUE_SHELL_TESTING")
    if verb is not None:
        environ["SSH_ORIGINAL_COMMAND"] = verb
    return subprocess.run(
        [str(SHELL), mode, str(conf or default_conf)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_valid_ro_verb_runs(env):
    r = run(env, "ro", "status")
    assert (r.returncode, r.stdout.strip()) == (0, "running")


def test_rw_verb_runs_with_rw_key(env):
    assert run(env, "rw", "restart").returncode == 0
    assert env[2].exists()


def test_rw_verb_with_ro_key_is_rejected_and_not_executed(env):
    r = run(env, "ro", "restart")
    assert r.returncode == 64
    assert not env[2].exists()


@pytest.mark.parametrize(
    "verb",
    ["rm", "status; id", "$(id)", "", "status\n", "status ", "Status", "a" * 40, "-status"],
)
def test_bad_verbs_are_rejected(env, verb):
    for mode in ("ro", "rw"):
        assert run(env, mode, verb).returncode == 64


def test_missing_ssh_original_command_is_rejected(env):
    assert run(env, "rw", None).returncode == 64


def test_invalid_key_mode_is_rejected(env):
    assert run(env, "admin", "status").returncode == 64


def test_output_is_capped(env):
    r = subprocess.run(
        [str(SHELL), "ro", str(env[1])],
        env={**env[0], "SSH_ORIGINAL_COMMAND": "big"},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert len(r.stdout) == 65536


def test_group_writable_catalogue_is_refused(env):
    conf = env[1]
    conf.chmod(0o664)
    r = run(env, "ro", "status")
    assert r.returncode != 0 and r.stdout == ""
    assert stat.S_IMODE(conf.stat().st_mode) & 0o020


def test_catalogue_not_owned_by_root_is_refused_without_testing_env(env):
    if os.geteuid() == 0:
        pytest.skip("el propietario es root")
    r = run(env, "ro", "status", testing=False)
    assert r.returncode != 0 and r.stdout == ""
