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


def test_sudo_guard_covers_absolute_path_arguments():
    from rescue_bot.presets import Action

    acts = [
        Action("fix", "rw", ("/bin/bash", "/srv/x/fix.sh", "--flag", "rel"), True),
        Action("st", "ro", ("/srv/y/untouched.sh", "/srv/y/data"), False),
    ]
    guard = enroll._sudo_guard(acts)
    assert "root_only_real /bin/bash\n" in guard
    assert "root_only_path /srv/x/fix.sh\n" in guard
    assert "--flag" not in guard and "rel" not in guard and "/srv/y" not in guard


def test_apply_declined_changes_nothing(config):
    runner = FakeRunner()
    with pytest.raises(enroll.EnrollError):
        enroll.enroll(load_app(config / "apps" / "example.yaml"), config, runner=runner,
                      confirm=lambda _: False, say=lambda _: None)  # fmt: skip
    assert all(call[1] is None for call in runner.calls)


def test_verify_only_makes_no_remote_changes(config, capsys):
    key = config / "keys" / "key_ro"
    calls = []

    def fake(_app, verb, used_key, _hosts):
        calls.append((verb, used_key))
        return Result(True, 0, "") if verb == "status" else Result(False, 64, "")

    app = load_app(config / "apps" / "example.yaml")
    enroll.verify(app, config, key, fake)
    # solo la clave ro y ningún verbo rw del catálogo: el rw se sondea con el verbo reservado
    assert calls == [(app.verbs("ro")[0], key), (enroll.RW_PROBE_VERB, key)]
    assert not set(app.verbs("rw")) & {verb for verb, _ in calls}


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


def test_sudo_argument_that_does_not_exist_yet_is_checked_through_its_ancestor():
    acts = [Action("fix", "rw", ("/bin/bash", "/srv/app/fix.sh"), True)]
    assert "root_only_path /srv/app/fix.sh\n" in enroll._sudo_guard(acts)


@pytest.mark.parametrize(
    "arg", ["-f/srv/app/fix.sh", "../srv/app/fix.sh", "/srv/app/../x", "/srv/./x"]
)
def test_sudo_rejects_paths_it_cannot_check(arg):
    with pytest.raises(enroll.EnrollError):
        enroll.sudo_paths([Action("fix", "rw", ("/bin/bash", arg), True)])


COMPOSE = {"kind": "docker-compose", "dir": "/opt/app", "file": "docker-compose.prod.yml"}


def compose_app(vm_actions=(), **preset):
    from types import SimpleNamespace

    from rescue_bot.presets import actions_for

    raw = {"preset": {**COMPOSE, **preset}, "vm_actions": list(vm_actions)}
    return SimpleNamespace(raw=raw, actions=actions_for(raw))


def test_compose_preset_uses_the_declared_file():
    restart = next(a for a in compose_app().actions if a.verb == "restart")
    assert restart.argv == (
        "/usr/bin/docker", "compose", "--project-directory", "/opt/app",
        "-f", "/opt/app/docker-compose.prod.yml", "restart",
    )  # fmt: skip


@pytest.mark.parametrize("bad", ["../etc/passwd", "sub/x.yml", ".hidden", ""])
def test_compose_file_must_be_a_plain_name(bad):
    from rescue_bot.presets import PresetError

    with pytest.raises(PresetError):
        compose_app(file=bad)


def test_compose_project_must_be_root_owned_by_default():
    app = compose_app()
    guard = enroll._compose_guard(app.raw["preset"])
    assert 'root_only_real "$p"' in guard and "owner_only" not in guard
    assert "/opt/app/docker-compose.prod.yml" in guard
    assert "root_only_path /opt/app/docker-compose.prod.yml\n" in enroll._sudo_guard(app.actions)


def test_remote_script_skips_sudo_checks_only_for_a_trusted_project():
    files = {path: ("x", "0644") for path in (enroll.SHELL_PATH, enroll.RESOURCES_PATH,
             enroll.ACTIONS_PATH, enroll.SUDOERS_PATH, enroll.KEYS_PATH)}  # fmt: skip
    plain = enroll.remote_script(compose_app(), files)
    trusted = enroll.remote_script(compose_app(trusted_owner="runner"), files)
    assert "root_only_path /opt/app\n" in plain
    assert "root_only_path /opt/app" not in trusted
    assert "root_only_real /usr/bin/docker\n" in trusted


def test_trusted_owner_relaxes_only_the_compose_project():
    app = compose_app(trusted_owner="runner")
    guard = enroll._compose_guard(app.raw["preset"])
    assert "trusted_uid=$(id -u runner)" in guard
    assert 'owner_only "$p" "$trusted_uid" /opt/app' in guard and "root_only_real" not in guard


@pytest.mark.parametrize("bad", ["Runner", "x;id", "", 7, "rescue"])
def test_trusted_owner_must_be_a_user_name(bad):
    with pytest.raises(enroll.EnrollError):
        enroll._compose_guard({**COMPOSE, "trusted_owner": bad})


# --- guard ejecutado en bash: árbol real en tmp, dueños y modos simulados -------------

RUNNER_UID = "987"
FAKE_TOOLS = r"""
stat() {  # stat -L -c %u|%a <ruta>: dueño y modo desde $TABLE (por defecto root 0755)
  awk -v p="$4" -v f="$3" '$1 == p { v = (f == "%u") ? $2 : $3 }
    END { print v ? v : (f == "%u" ? 0 : 755) }' "$TABLE"
}
id() { echo """ + RUNNER_UID + r"""; }
"""


def run_guard(tmp_path, app, owners):
    """Ejecuta los guards de remote_script sobre `app`; owners: ruta → (uid, modo)."""
    table = tmp_path / "table"
    table.write_text("".join(f"{p} {u} {m}\n" for p, (u, m) in owners.items()))
    preset = app.raw["preset"]
    trusted = enroll._trusted_owner(preset) is not None
    skip = frozenset(enroll._compose_paths(preset)) if trusted else frozenset()
    # /usr/bin/docker puede no existir aquí; su comprobación la cubren los tests de texto.
    sudo = enroll._sudo_guard(app.actions, skip).replace("root_only_real /usr/bin/docker\n", "")
    script = "".join([
        "set -euo pipefail\n", 'die() { echo "enroll: $*" >&2; exit 1; }\n', FAKE_TOOLS,
        enroll.ROOT_ONLY_FN, sudo, enroll._compose_guard(preset), 'echo "guard: ok"\n',
    ])  # fmt: skip
    env = {"TABLE": str(table), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture
def project(tmp_path):
    root = (tmp_path / "opt" / "app").resolve()
    (root / "scripts").mkdir(parents=True)
    (root / "docker-compose.prod.yml").write_text("services: {}\n")
    (root / "scripts" / "fix.sh").write_text("#!/bin/sh\n")
    return root


def compose_file(project):
    return f"{project}/docker-compose.prod.yml"


def app_at(project, **kw):
    return compose_app(dir=str(project), **kw)


def test_bash_guard_accepts_a_project_owned_by_the_trusted_user(tmp_path, project):
    owners = {str(project): (RUNNER_UID, 755), compose_file(project): (RUNNER_UID, 644)}
    r = run_guard(tmp_path, app_at(project, trusted_owner="runner"), owners)
    assert r.returncode == 0, r.stderr


def test_bash_guard_rejects_the_same_project_without_trusted_owner(tmp_path, project):
    owners = {str(project): (RUNNER_UID, 755), compose_file(project): (RUNNER_UID, 644)}
    r = run_guard(tmp_path, app_at(project), owners)
    assert r.returncode != 0 and "no es de root" in r.stderr


def test_bash_guard_rejects_compose_file_of_another_user(tmp_path, project):
    owners = {str(project): (RUNNER_UID, 755), compose_file(project): ("1000", 644)}
    r = run_guard(tmp_path, app_at(project, trusted_owner="runner"), owners)
    assert r.returncode != 0 and "dueño de confianza" in r.stderr


def test_bash_guard_still_checks_other_sudo_paths_inside_a_trusted_project(tmp_path, project):
    fix = f"{project}/scripts/fix.sh"
    vm = [{"verb": "fix", "mode": "rw", "command": ["/bin/bash", fix], "sudo": True}]
    owners = {str(project): (RUNNER_UID, 755), fix: ("1000", 666)}
    r = run_guard(tmp_path, app_at(project, trusted_owner="runner", vm_actions=vm), owners)
    assert r.returncode != 0 and fix in r.stderr
