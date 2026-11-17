"""Matriz scenario -> test de docs/specs/rescue-bot-v1.md (Criterios de aceptación).

1 alerta a Slack sin bot: MANUAL/E2E (integración nativa de Healthchecks, sin código)
2 diagnóstico solo ro: test_hermes_plugin.py; aquí test_diagnose_*
3 clave ro no escribe: test_rescue_shell.py; aquí test_ro_key_*
4 verbo inexistente: test_rescue_shell.py; aquí test_unknown_verbs_*
5 aprobación válida: test_approver.py; aquí test_valid_approval_*
6 no autorizada/caducada/repetida: test_approver.py (todas las variantes)
7 propuesta inválida: test_approver.py, test_approver_server.py
8 límite de acciones: test_approver.py; aquí test_rate_limit_*
9 repo público limpio: aquí test_public_repo_* (gitleaks corre en la CI)
"""

import importlib.util
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, SHELL
from test_approver_support import APPROVER, CHANNEL, Env

from rescue_bot import ssh
from rescue_bot.approver.executor import Executor
from rescue_bot.health import Health
from rescue_bot.ssh import Result

PLUGIN = ROOT / "engines" / "hermes" / "plugin" / "rescue"


def run_shell(vm, mode, command):
    env = dict(vm.env)
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [str(SHELL), mode, str(vm.conf)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# --- escenario 3 ----------------------------------------------------------


def test_ro_key_rejects_every_rw_verb_of_the_example_app(vm, example_app):
    rw_verbs = example_app.verbs("rw")
    assert "restart" in rw_verbs
    for verb in rw_verbs:
        r = run_shell(vm, "ro", verb)
        assert r.returncode == 64, verb
    assert vm.markers() == []


def test_ro_key_rejection_is_logged_to_syslog(vm):
    run_shell(vm, "ro", "restart")
    assert "verb=restart" in vm.log() and "rejected" in vm.log()


def test_rw_key_still_runs_rw_verbs_and_ro_key_runs_ro_verbs(vm):
    assert run_shell(vm, "rw", "restart").returncode == 0
    assert run_shell(vm, "ro", "status").returncode == 0
    assert vm.markers() == ["restart", "status"]


# --- escenario 4 ----------------------------------------------------------


@pytest.mark.parametrize(
    "verb", ["rm", "bash", "status; id", "status && id", "$(id)", "status\nid"]
)
@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_unknown_verbs_are_rejected_without_executing_anything(vm, mode, verb):
    assert run_shell(vm, mode, verb).returncode == 64
    assert vm.markers() == []
    assert "rejected" in vm.log()


# --- escenario 2 ----------------------------------------------------------


@pytest.fixture
def plugin_tools(monkeypatch):
    monkeypatch.setenv("RESCUE_CONFIG", str(ROOT / "examples" / "config"))
    monkeypatch.setenv("RESCUE_KEY_RO", "/nonexistent/key_ro")
    name = "rescue_plugin_scenarios"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{name}.tools"]


def test_diagnose_reaches_ssh_only_with_ro_verbs_and_the_ro_key(
    plugin_tools, example_app, monkeypatch
):
    calls = []

    def fake_run(app, verb, key, known_hosts, **kw):
        calls.append((verb, str(key)))
        return Result(True, 0, "ok")

    monkeypatch.setattr(ssh, "run_verb", fake_run)
    every_verb = [*example_app.verbs(), "rm", "bash", "status; id"]
    for verb in every_verb:
        json.loads(plugin_tools.diagnose({"app": "example", "verb": verb}))
    assert [v for v, _ in calls] == example_app.verbs("ro")
    assert {k for _, k in calls} == {"/nonexistent/key_ro"}


# --- escenario 5 (cadena: Approver real + Executor real; fakes en Slack, ssh y salud) ----


def test_valid_approval_runs_with_rw_key_then_checks_health_and_reports(tmp_path):
    steps = []

    def fake_ssh(app, verb, key, known_hosts):
        steps.append(("ssh", app.name, verb, key.name))
        return Result(True, 0, "reiniciado")

    def fake_health(app):
        steps.append(("health", app.name))
        return Health(True, 200, "HTTP 200")

    executor = Executor(Path("key_rw"), Path("known_hosts"), run=fake_ssh, wait_healthy=fake_health)
    env = Env(tmp_path, executor)
    pid = env.propose("restart")
    env.approver.decide(pid, APPROVER, CHANNEL, "1.2", True)

    assert steps == [("ssh", "example", "restart", "key_rw"), ("health", "example")]
    report = env.slack.posted[-1]["text"]
    assert "reiniciado" in report and "HTTP 200" in report
    assert env.events() == ["proposed", "executing", "executed"]


# --- escenario 8 (cadena: tres ejecuciones reales vía clic → la cuarta se bloquea) ----


def test_rate_limit_blocks_after_three_approved_actions_in_an_hour(tmp_path):
    env = Env(tmp_path)
    for _ in range(3):
        env.propose()  # Env.propose devuelve el primer botón; el de esta ronda es el último
        pid = env.slack.posted[-1]["blocks"][-1]["elements"][0]["value"]
        env.approver.decide(pid, APPROVER, CHANNEL, "1.2", True)
        env.clock.now += 400
    reply = env.approver.submit(b'{"app":"example","verb":"restart","reason":"otra"}')
    assert len(env.executor.calls) == 3
    assert reply["ok"] is False and "manual" in reply["error"]


# --- escenario 9 ----------------------------------------------------------

ALLOWED_IP = ipaddress.ip_address("100.64.0.10")
CGNAT = ipaddress.ip_network("100." + "64.0.0/10")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]|/\d)")  # `/n`: CIDR, no host
TS_NET = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\.ts\.net\b")
ALLOWED_TS_NET = {"example.ts.net", "tailnet-xyz.ts.net"}
SLACK_ID = re.compile(r"\b[CUWTB](?=[A-Z0-9]*\d)[A-Z0-9]{8,10}\b")
ALLOWED_SLACK_IDS = {"C0000000000", "C0000000001", "U0000000000", "U0000000001"}


def tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    for rel in filter(None, out.split("\0")):
        path = ROOT / rel
        if path.is_file():
            try:
                yield rel, path.read_text()
            except UnicodeDecodeError:
                continue


def findings():
    found = []
    for rel, text in tracked_text_files():
        for m in IPV4.finditer(text):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if ip in CGNAT and ip != ALLOWED_IP:
                found.append((rel, "tailscale-ip", m.group(1)))
        for m in TS_NET.finditer(text):
            host = m.group(0)
            if not any(host == a or host.endswith("." + a) for a in ALLOWED_TS_NET):
                found.append((rel, "ts.net", host))
        for m in SLACK_ID.finditer(text):
            if m.group(0) not in ALLOWED_SLACK_IDS:
                found.append((rel, "slack-id", m.group(0)))
    return found


@pytest.mark.parametrize("kind", ["tailscale-ip", "ts.net", "slack-id"])
def test_public_repo_has_no_real_infrastructure_data(kind):
    assert [f for f in findings() if f[1] == kind] == []


def test_public_repo_scanner_flags_what_it_should(monkeypatch):
    bad_ip, bad_host, bad_id = "100." + "99.12.7", "db.tailnet-abc" + ".ts.net", "C01" + "ABCDEF23"
    cidr = "100." + "64.0.0/10"
    sample = f"ip {bad_ip} net {cidr} ok 100.64.0.10 host {bad_host} ok a.example.ts.net "
    sample += f"id {bad_id} ok U0000000000"
    monkeypatch.setattr(sys.modules[__name__], "tracked_text_files", lambda: [("x", sample)])
    assert [(k, v) for _, k, v in findings()] == [
        ("tailscale-ip", bad_ip),
        ("ts.net", bad_host),
        ("slack-id", bad_id),
    ]
