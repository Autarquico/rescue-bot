import re
import subprocess
from pathlib import Path

import pytest

from rescue_bot import enroll
from rescue_bot.config import load_app
from rescue_bot.presets import Action
from rescue_bot.ssh import Result

ACTIONS = [
    Action("status", "ro", ("/usr/bin/systemctl", "status", "--no-pager", "x.service")),
    Action("restart", "rw", ("/usr/bin/systemctl", "restart", "x.service"), sudo=True),
]
RO = "ssh-ed25519 AAAAro rescue-ro"
RW = "ssh-ed25519 AAAArw rescue-rw"


def test_actions_conf():
    assert enroll.render_actions_conf(ACTIONS).splitlines()[2:] == [
        "status ro /usr/bin/systemctl status --no-pager x.service",
        "restart rw sudo /usr/bin/systemctl restart x.service",
    ]


def test_sudoers_exact_lines_only():
    text = enroll.render_sudoers("x", ACTIONS)
    rules = [line for line in text.splitlines() if not line.startswith("#")]
    assert rules == ["rescue ALL=(root) NOPASSWD: /usr/bin/systemctl restart x.service"]
    assert not re.search(r"[*?,:\\]", "".join(rules).replace("NOPASSWD:", "").replace("ALL=", ""))


def test_authorized_keys_exact_lines():
    assert enroll.render_authorized_keys("100.64.0.10", RO, RW).splitlines() == [
        f'restrict,from="100.64.0.10",command="/usr/lib/rescue/rescue-shell ro" {RO}',
        f'restrict,from="100.64.0.10",command="/usr/lib/rescue/rescue-shell rw" {RW}',
    ]


@pytest.mark.parametrize("bad", ["", 'ssh-ed25519 AAAA"x', "ssh-ed25519 AAAA\nrestrict x"])
def test_authorized_keys_rejects_bad_keys(bad):
    with pytest.raises(enroll.EnrollError):
        enroll.render_authorized_keys("100.64.0.10", bad, RW)


def test_authorized_keys_rejects_bad_ip():
    with pytest.raises(ValueError):
        enroll.render_authorized_keys('1.2.3.4",command="x', RO, RW)


class FakeRunner:
    def __init__(self, current=None):
        self.calls = []
        self.current = current  # None → los ficheros no existen

    def run(self, argv, *, input=None, timeout=120):
        self.calls.append((list(argv), input))
        if input is None:
            code, out = (3, "") if self.current is None else (0, self.current)
            return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "enroll: ok\n", "")


@pytest.fixture
def config(tmp_path):
    src = Path(__file__).resolve().parent.parent / "examples" / "config"
    (tmp_path / "apps").mkdir()
    (tmp_path / "settings.yaml").write_text((src / "settings.yaml").read_text())
    (tmp_path / "apps" / "example.yaml").write_text((src / "apps" / "example.yaml").read_text())
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "key_ro.pub").write_text(RO + "\n")
    (tmp_path / "keys" / "key_rw.pub").write_text(RW + "\n")
    return tmp_path


def test_dry_run_prints_diff_and_applies_nothing(config):
    runner, out = FakeRunner(), []
    enroll.enroll(load_app(config / "apps" / "example.yaml"), config, dry_run=True,
                  runner=runner, say=out.append)  # fmt: skip
    assert "+rescue ALL=(root) NOPASSWD: /usr/bin/systemctl restart example.service" in out[0]
    assert all(call[1] is None for call in runner.calls)


def test_apply_sends_idempotent_script_over_stdin(config):
    runner = FakeRunner()
    enroll.enroll(load_app(config / "apps" / "example.yaml"), config, yes=True,
                  runner=runner, say=lambda _: None)  # fmt: skip
    argv, script = runner.calls[-1]
    assert argv[-2:] == ["root@example.example.ts.net", "bash -s"]
    for needle in ("useradd --system", "visudo -cf", "-m 0440", "-m 0755 /home/rescue"):
        assert needle in script


def test_remote_script_checks_ownership_of_every_sudo_executable(config):
    app = load_app(config / "apps" / "example.yaml")
    script = enroll.remote_script(app, enroll.desired_files(app, config))
    assert "stat -L -c %u" in script and "8#022" in script and '[ "$p" = / ] && break' in script
    sudo_exes = {a.argv[0] for a in app.actions if a.sudo}
    assert sudo_exes
    for exe in sudo_exes:
        assert f"root_only_real {exe}\n" in script
    # los guards van antes de cualquier cambio en la VM
    assert script.index(f"root_only_real {sorted(sudo_exes)[0]}") < script.index("useradd")
    # y tras instalar rescue-shell se comprueba ella y su directorio
    tail = script[script.index(f"install -o root -g root -m 0755 \"$tmp\" {enroll.SHELL_PATH}") :]
    assert f"root_only_real {enroll.SHELL_PATH}" in tail
    assert "root_only_real /usr/lib/rescue" in tail
    assert tail.index("root_only_real /usr/lib/rescue") < tail.index(enroll.KEYS_PATH)


def test_apply_declined_changes_nothing(config):
    runner = FakeRunner()
    with pytest.raises(enroll.EnrollError):
        enroll.enroll(load_app(config / "apps" / "example.yaml"), config, runner=runner,
                      confirm=lambda _: False, say=lambda _: None)  # fmt: skip
    assert all(call[1] is None for call in runner.calls)


def test_verify_only_makes_no_remote_changes(config, capsys):
    key = config / "keys" / "key_ro"
    calls = []

    def fake(_app, verb, _key, _hosts):
        calls.append(verb)
        return Result(True, 0, "") if verb == "status" else Result(False, 64, "")

    app = load_app(config / "apps" / "example.yaml")
    enroll.verify(app, config, key, fake)
    assert calls == [*app.verbs("ro")[:1], *app.verbs("rw")]


def test_verify_only_flag_requires_key(config):
    assert enroll.main(["example", "--config", str(config), "--verify-only"]) == 1


def test_verify_requires_ro_ok_and_rw_rejected(config):
    app = load_app(config / "apps" / "example.yaml")
    key = config / "keys" / "key_ro"

    def fake(ok_status, restart_code):
        def run(_app, verb, _key, _hosts):
            return (
                Result(True, 0, "")
                if verb == "status" and ok_status
                else (
                    Result(False, restart_code, "") if verb != "status" else Result(False, 255, "")
                )
            )

        return run  # fmt: skip

    enroll.verify(app, config, key, fake(True, 64))
    with pytest.raises(enroll.EnrollError):
        enroll.verify(app, config, key, fake(False, 64))
    with pytest.raises(enroll.EnrollError):
        enroll.verify(app, config, key, fake(True, 0))
