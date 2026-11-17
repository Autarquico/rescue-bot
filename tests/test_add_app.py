import json
import subprocess
from pathlib import Path

import pytest

from rescue_bot import add_app, enroll
from rescue_bot.config import ConfigError, load_app
from rescue_bot.health import Health

SCAN = "web.example.ts.net ssh-ed25519 AAAAhostkey\n"


class FakeRunner:
    def __init__(self, tailscale=None):
        self.calls = []
        self.posts = []
        self.tailscale = tailscale

    def run(self, argv, *, input=None, timeout=120):
        self.calls.append(list(argv))
        out = SCAN if argv[0] == "ssh-keyscan" else "256 SHA256:abc web (ED25519)\n"
        if argv[:2] == ["tailscale", "status"]:
            out = json.dumps(self.tailscale)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def which(self, name):
        return "/usr/bin/tailscale" if self.tailscale is not None else None

    def post_json(self, url, headers, body):
        self.posts.append((url, headers, body))
        return {"ping_url": "https://hc-ping.com/abc"}


@pytest.fixture
def config(tmp_path):
    src = Path(__file__).resolve().parent.parent / "examples" / "config"
    (tmp_path / "settings.yaml").write_text((src / "settings.yaml").read_text())
    (tmp_path / ".git").mkdir()
    return tmp_path


def args(config, *extra):
    return add_app.parser().parse_args(
        ["web", "--config", str(config), "--host", "web.example.ts.net", "--preset", "systemd",
         "--unit", "web.service", "--health-url", "https://web.example.invalid/healthz",
         "--yes", *extra]
    )  # fmt: skip


def go(config, *extra, runner=None, env=None):
    runner = runner or FakeRunner()
    enrolled, said = [], []

    def fake_enroll(app, cfg, **kw):
        enrolled.append((app.name, kw.get("dry_run", False)))

    console = add_app.Console(yes=True)
    console.say = said.append
    add_app.add_app(
        args(config, *extra), runner=runner, console=console, env=env or {},
        enroll_fn=fake_enroll, check_health=lambda app: Health(True, 200, "HTTP 200"),
    )  # fmt: skip
    return runner, enrolled, said


def test_happy_path(config):
    runner, enrolled, said = go(config, env={"HC_API_KEY": "k"})
    app = load_app(config / "apps" / "web.yaml")
    assert app.verbs() == ["status", "logs", "resources", "restart"]
    assert (config / "known_hosts").read_text() == SCAN
    assert enrolled == [("web", True), ("web", False)]
    git = [c for c in runner.calls if c[0] == "git"]
    assert [c[3] for c in git] == ["add", "commit", "push"]
    assert runner.calls[-1][0] == "ssh"
    assert runner.calls[-1][1] == "root@rescue.example.ts.net"
    assert "systemctl restart rescue-approver hermes-gateway" in runner.calls[-1][2]
    url, headers, body = runner.posts[0]
    assert url == "https://healthchecks.io/api/v3/checks/"
    assert body == {"name": "web-up", "unique": ["name"], "channels": "*"}
    assert any("hc-ping.com/abc" in line for line in said)
    assert any(line.startswith("smoke: ok") for line in said)


def test_no_reload_and_no_healthcheck(config):
    runner, _, _ = go(config, "--no-reload", "--no-healthcheck", env={"HC_API_KEY": "k"})
    assert not runner.posts
    assert not any(c[0] == "ssh" for c in runner.calls)


def test_known_hosts_is_deduplicated_and_changed_key_refused(config):
    go(config)
    (config / "apps" / "web.yaml").unlink()
    go(config)
    assert (config / "known_hosts").read_text() == SCAN

    (config / "apps" / "web.yaml").unlink()

    class Changed(FakeRunner):
        def run(self, argv, **kw):
            proc = super().run(argv, **kw)
            if argv[0] == "ssh-keyscan":
                proc.stdout = "web.example.ts.net ssh-ed25519 AAAAother\n"
            return proc

    with pytest.raises(add_app.AddAppError):
        go(config, runner=Changed())


def test_invalid_preset_param_is_rejected_and_yaml_removed(config):
    bad = add_app.parser().parse_args(
        ["web", "--config", str(config), "--host", "h.example.ts.net", "--preset", "systemd",
         "--unit", "web.service; rm -rf /", "--health-url", "https://x.invalid/h", "--yes"]
    )  # fmt: skip
    with pytest.raises(ConfigError):
        add_app.add_app(bad, runner=FakeRunner(), console=add_app.Console(yes=True), env={})
    assert not (config / "apps" / "web.yaml").exists()


def test_yes_fails_on_missing_values(config):
    incomplete = add_app.parser().parse_args(["web", "--config", str(config), "--yes"])
    with pytest.raises(add_app.AddAppError):
        add_app.add_app(incomplete, runner=FakeRunner(), console=add_app.Console(yes=True), env={})


@pytest.mark.parametrize(
    ("peers", "expected"),
    [
        ({"a": {"DNSName": "web.example.ts.net.", "Tags": ["tag:app"]}}, "tiene tag:app"),
        (
            {"a": {"DNSName": "web.example.ts.net.", "Tags": []}},
            "tailscale set --advertise-tags=tag:app",
        ),
        ({}, "no aparece"),
    ],
)
def test_tailscale_tag_check(peers, expected):
    msg = add_app.check_tailscale_tag("web.example.ts.net", FakeRunner(tailscale={"Peer": peers}))
    assert expected in msg


def test_tailscale_missing_warns_with_command():
    assert "tag:app" in add_app.check_tailscale_tag("web.example.ts.net", FakeRunner())


def test_enroll_runner_is_shared():
    assert add_app.Runner is enroll.Runner
