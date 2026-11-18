import json
import subprocess
from pathlib import Path

import pytest

from rescue_bot import add_app, enroll
from rescue_bot.config import ConfigError, load_app
from rescue_bot.health import Health

SCAN = "web.example.ts.net ssh-ed25519 AAAAhostkey\n"


class FakeRunner:
    def __init__(self, tailscale=None, staged=True):
        self.calls = []
        self.staged = staged
        self.posts = []
        self.tailscale = tailscale

    def run(self, argv, *, input=None, timeout=120):
        self.calls.append(list(argv))
        out = SCAN if argv[0] == "ssh-keyscan" else "256 SHA256:abc web (ED25519)\n"
        if argv[:2] == ["tailscale", "status"]:
            out = json.dumps(self.tailscale)
        code = int(self.staged) if argv[3:5] == ["diff", "--cached"] else 0
        return subprocess.CompletedProcess(argv, code, out, "")

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
    assert [c[3] for c in git] == ["add", "diff", "commit", "push"]
    ssh = [c for c in runner.calls if c[0] == "ssh"]
    assert ssh == [
        ["ssh", "root@rescue.example.ts.net", "/usr/local/sbin/rescue-reload"],
        ["ssh", "root@rescue.example.ts.net", "/usr/local/sbin/rescue-verify web"],
    ]
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
    go(config, "--update")
    assert (config / "known_hosts").read_text() == SCAN

    class Changed(FakeRunner):
        def run(self, argv, **kw):
            proc = super().run(argv, **kw)
            if argv[0] == "ssh-keyscan":
                proc.stdout = "web.example.ts.net ssh-ed25519 AAAAother\n"
            return proc

    with pytest.raises(add_app.AddAppError):
        go(config, "--update", runner=Changed())


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_dry_run_has_no_side_effects(config):
    before = snapshot(config)
    runner, enrolled, said = go(config, "--dry-run", env={"HC_API_KEY": "k"})
    assert snapshot(config) == before
    assert not (config / "apps").exists() or not list((config / "apps").iterdir())
    assert enrolled == [("web", True)]
    assert {c[0] for c in runner.calls} <= {"ssh-keyscan", "ssh-keygen"}
    assert not runner.posts
    text = "\n".join(said)
    assert "name: web" in text and "SHA256:abc" in text


def test_failed_enroll_leaves_no_ficha_and_retry_works(config):
    def boom(app, cfg, **kw):
        if not kw.get("dry_run"):
            raise enroll.EnrollError("falló")

    console = add_app.Console(yes=True)
    console.say = lambda _: None
    with pytest.raises(enroll.EnrollError):
        add_app.add_app(args(config), runner=FakeRunner(), console=console, env={}, enroll_fn=boom)
    assert not (config / "apps" / "web.yaml").exists()
    assert not (config / "known_hosts").exists()
    go(config)
    assert (config / "apps" / "web.yaml").exists()


def test_existing_ficha_needs_update(config):
    go(config)
    with pytest.raises(add_app.AddAppError):
        go(config)


def test_verify_failure_in_ct_is_an_error(config):
    class Failing(FakeRunner):
        def run(self, argv, **kw):
            proc = super().run(argv, **kw)
            if argv[:2] == ["ssh", "root@rescue.example.ts.net"] and "verify" in argv[2]:
                proc.returncode = 1
            return proc

    with pytest.raises(add_app.AddAppError):
        go(config, runner=Failing())


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


def test_unchanged_config_skips_commit_but_still_pushes(config):
    from rescue_bot.add_app import commit_config

    (config / ".git").mkdir(exist_ok=True)
    runner = FakeRunner(staged=False)
    assert commit_config(config, "web", runner)
    assert [c[3] for c in runner.calls] == ["add", "diff", "push"]


def test_app_names_match_the_ct_verify_pattern():
    import pytest as _pytest

    from rescue_bot.add_app import AddAppError, render_ficha

    for bad in ["My.App", "-x", "a" * 33, "1app"]:
        with _pytest.raises(AddAppError):
            render_ficha(bad, {"preset": "systemd", "target": "web.service"})


def test_ficha_carries_compose_file_and_trusted_owner():
    import yaml

    from rescue_bot.add_app import render_ficha

    values = {
        "preset": "docker-compose", "target": "/opt/app", "file": "docker-compose.prod.yml",
        "trusted_owner": "runner", "url": "https://app.example", "health_url": "https://app.example/health",
        "expect": "200", "host": "app.example.ts.net", "admin": "root",
    }  # fmt: skip
    preset = yaml.safe_load(render_ficha("app", values))["preset"]
    assert preset == {
        "kind": "docker-compose", "dir": "/opt/app",
        "file": "docker-compose.prod.yml", "trusted_owner": "runner",
    }  # fmt: skip


def test_ficha_splits_several_trusted_owners_and_keeps_the_group():
    import yaml

    from rescue_bot.add_app import render_ficha

    values = {
        "preset": "docker-compose", "target": "/opt/app", "trusted_owner": "deploy,runner",
        "trusted_group": "team", "url": "https://app.example",
        "health_url": "https://app.example/health", "expect": "200",
        "host": "app.example.ts.net", "admin": "root",
    }  # fmt: skip
    preset = yaml.safe_load(render_ficha("app", values))["preset"]
    assert preset["trusted_owner"] == ["deploy", "runner"] and preset["trusted_group"] == "team"
