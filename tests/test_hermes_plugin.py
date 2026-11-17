import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest
import yaml

from rescue_bot import ssh
from rescue_bot.ssh import Result

ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "engines" / "hermes"
PLUGIN = HERMES / "plugin" / "rescue"
EXAMPLES = ROOT / "examples" / "config"


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location(
        "rescue_plugin", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["rescue_plugin"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tools(plugin, monkeypatch):
    monkeypatch.setenv("RESCUE_CONFIG", str(EXAMPLES))
    monkeypatch.setenv("RESCUE_KEY_RO", "/nonexistent/key_ro")

    def no_ssh(*a, **k):
        raise AssertionError("ssh no debe llamarse")

    monkeypatch.setattr(ssh, "run_verb", no_ssh)
    return sys.modules["rescue_plugin.tools"]


def call(handler, **args):
    out = handler(args)
    assert isinstance(out, str)
    return json.loads(out)


def test_register_exposes_rescue_toolset(plugin):
    registered = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    plugin.register(Ctx())
    meta = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert {t["name"] for t in registered} == set(meta["provides_tools"])
    assert all(t["toolset"] == "rescue" and callable(t["handler"]) for t in registered)
    assert all(t["schema"]["name"] == t["name"] for t in registered)


def test_template_enables_only_rescue_on_slack():
    cfg = yaml.safe_load((HERMES / "config.yaml.tmpl").read_text())
    assert cfg["platform_toolsets"] == {"slack": ["rescue"]}
    disabled = set(cfg["agent"]["disabled_toolsets"])
    assert {"terminal", "file", "code_execution", "web", "browser"} <= disabled
    assert "rescue" not in disabled
    assert cfg["model"]["provider"] == "anthropic"
    assert cfg["slack"]["require_mention"] is True


def test_list_apps(tools):
    apps = call(tools.list_apps)["apps"]
    assert apps[0]["name"] == "example"
    assert "restart" in apps[0]["rw"] and "status" in apps[0]["ro"]
    assert apps[0]["has_runbook"] is True


@pytest.mark.parametrize("verb", ["restart", "rollback", "rm", "status; id", None])
def test_diagnose_rejects_non_ro_without_ssh(tools, verb):
    out = call(tools.diagnose, app="example", verb=verb)
    assert out["ok"] is False


def test_diagnose_unknown_app(tools):
    assert call(tools.diagnose, app="nope", verb="status")["ok"] is False


def test_diagnose_redacts_and_wraps(tools, monkeypatch):
    key = "ghp_" + "a" * 36
    monkeypatch.setattr(
        ssh, "run_verb", lambda app, verb, k, kh: Result(True, 0, f"active\ntoken={key}")
    )
    out = call(tools.diagnose, app="example", verb="status")
    assert key not in out["untrusted_output"] and "active" in out["untrusted_output"]
    assert "no fiable" in out["note"]


def test_handlers_never_raise(tools):
    assert json.loads(tools.diagnose(None))["ok"] is False
    assert json.loads(tools.request_action({"app": 1}, extra=True))["ok"] is False


def test_request_action_only_uses_socket(tools, monkeypatch, tmp_path):
    monkeypatch.setenv("RESCUE_KEY_RW", str(tmp_path / "key_rw"))
    opened = []
    import builtins

    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
    )
    path = Path("/tmp") / f"rb-{threading.get_ident()}.sock"  # noqa: S108
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    seen = []

    def serve():
        conn, _ = srv.accept()
        seen.append(conn.makefile("rb").readline())
        conn.sendall(b'{"ok": true, "message": "publicada"}\n')
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    monkeypatch.setenv("RESCUE_SOCKET", str(path))
    out = call(tools.request_action, app="example", verb="restart", reason="cae")
    t.join()
    srv.close()
    path.unlink()
    assert out == {"ok": True, "message": "publicada"}
    assert json.loads(seen[0]) == {"app": "example", "verb": "restart", "reason": "cae"}
    assert not any("key_rw" in f for f in opened)


def test_request_action_without_socket_returns_error(tools, monkeypatch, tmp_path):
    monkeypatch.setenv("RESCUE_SOCKET", str(tmp_path / "missing.sock"))
    assert call(tools.request_action, app="example", verb="restart", reason="x")["ok"] is False


@pytest.mark.parametrize(
    "secret",
    [
        "xoxb-123456789012-abcdefghij",
        "xapp-1-A0123456789-abcdef",
        "ghp_" + "A" * 36,
        "github_pat_" + "B" * 30,
        "AKIAIOSFODNN7EXAMPLE",
        "sk-ant-api03-abcdefgh12345678",
        "sk-" + "c" * 30,
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----",
        "password=hunter2",
        "secret: s3cr3t",
        "Authorization: Bearer abcdef0123456789",
        "Bearer abcdef0123456789",
    ],
)
def test_redact_masks(plugin, secret):
    redact = sys.modules["rescue_plugin.redact"].redact
    out = redact(f"antes {secret} después")
    assert "[REDACTED]" in out and "antes" in out and "después" in out
    for fragment in ("hunter2", "s3cr3t", "abcdef0123456789", "IOSFODNN7", "abc\ndef"):
        assert fragment not in out


def test_redact_keeps_plain_text(plugin):
    redact = sys.modules["rescue_plugin.redact"].redact
    assert redact("nginx.service active (running)") == "nginx.service active (running)"
