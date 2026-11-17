import json
import threading

import pytest
from test_approver_support import APPROVER, CHANNEL, Env, FakeExecutor


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def click(env, pid, user=APPROVER, channel=CHANNEL, approve=True):
    env.approver.decide(pid, user, channel, "111.222", approve)


# scenario 5
def test_valid_approval_executes_once_and_posts_result(env):
    pid = env.propose()
    assert set(env.slack.buttons()[0]) >= {"value"}
    assert all(b["value"] == pid for b in env.slack.buttons())
    click(env, pid)
    assert env.executor.calls == [("example", "restart")]
    assert env.slack.updated[-1]["blocks"] == []
    result = env.slack.posted[-1]
    assert result["thread_ts"] == "111.222" and "salida" in result["text"]
    assert env.events() == ["proposed", "executing", "executed"]


def test_reject_by_approver_consumes(env):
    pid = env.propose()
    click(env, pid, approve=False)
    click(env, pid)
    assert env.executor.calls == []
    assert env.events() == ["proposed", "declined", "denied_approval"]


def test_failed_execution_counts_toward_limit(tmp_path):
    env = Env(tmp_path, FakeExecutor(code=1))
    click(env, env.propose())
    assert "falló" in env.slack.posted[-1]["text"]
    assert env.approver.submit(b'{"app":"example","verb":"restart","reason":"x"}')["ok"] is False


# scenario 6
def test_non_approver_does_not_execute(env):
    pid = env.propose()
    click(env, pid, user="U0000000001")
    assert env.executor.calls == []
    assert env.events()[-1] == "denied_approval"
    entry = json.loads(env.audit._path.read_text().splitlines()[-1])
    assert entry["user"] == "U0000000001" and env.slack.ephemeral
    click(env, pid)  # el intento ajeno no consumió la propuesta
    assert env.executor.calls == [("example", "restart")]


def test_expired(env):
    pid = env.propose()
    env.clock.now += 601
    click(env, pid)
    assert env.executor.calls == [] and env.events()[-1] == "denied_approval"


def test_double_click(env):
    pid = env.propose()
    click(env, pid)
    click(env, pid)
    assert env.executor.calls == [("example", "restart")]
    assert env.events() == ["proposed", "executing", "executed", "denied_approval"]


def test_concurrent_double_click(tmp_path):
    gate = threading.Event()
    env = Env(tmp_path, FakeExecutor(gate=gate))
    pid = env.propose()
    threads = [threading.Thread(target=click, args=(env, pid)) for _ in range(2)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert len(env.executor.calls) == 1
    assert env.events().count("denied_approval") == 1


def test_wrong_channel(env):
    pid = env.propose()
    click(env, pid, channel="C0000000001")
    assert env.executor.calls == [] and env.events()[-1] == "denied_approval"


def test_changed_config_digest(env):
    pid = env.propose()
    (env.config_dir / "settings.yaml").write_text(
        (env.config_dir / "settings.yaml").read_text() + "\n# cambio\n"
    )
    click(env, pid)
    assert env.executor.calls == [] and env.events()[-1] == "denied_approval"


def test_unknown_proposal_after_restart(env):
    click(env, "no-existe")
    assert env.executor.calls == [] and env.events() == ["denied_approval"]


# scenario 7
@pytest.mark.parametrize(
    "line",
    [
        b'{"app":"nope","verb":"restart","reason":"x"}',
        b'{"app":"example","verb":"rm","reason":"x"}',
        b'{"app":"example","verb":"status","reason":"x"}',
        b'{"app":"example","verb":"restart","reason":"x","extra":1}',
        b'{"app":"example","verb":"restart"}',
        b'{"app":"example","verb":"restart","reason":"' + b"a" * 501 + b'"}',
        b'{"app":"example","verb":"status; id","reason":"x"}',
        b"no es json",
        b"[]",
        b'{"app":"example","verb":"restart","reason":"' + b"a" * 5000 + b'"}',
    ],
)
def test_invalid_proposals_never_reach_slack(env, line):
    reply = env.approver.submit(line)
    assert reply["ok"] is False
    assert env.slack.posted == []
    assert env.events() == ["rejected_proposal"]


# scenario 8
def _executed(env, ago_s):
    env.clock.now -= ago_s
    env.audit.write("executed", app="example", verb="restart")
    env.clock.now += ago_s


def test_fourth_action_in_an_hour_is_blocked(env):
    for ago in (3000, 2000, 1000):
        _executed(env, ago)
    reply = env.approver.submit(b'{"app":"example","verb":"restart","reason":"x"}')
    assert reply["ok"] is False and "manual" in reply["error"]
    assert env.slack.buttons() == []
    assert env.slack.posted == []
    assert env.events()[-1] == "blocked_proposal"


def test_blocked_proposals_never_post_to_slack(env):
    _executed(env, 100)
    assert not any(env.approver.submit(
        b'{"app":"example","verb":"restart","reason":"x"}')["ok"] for _ in range(500))  # fmt: skip
    assert env.slack.posted == []


def test_action_within_min_interval_is_blocked(env):
    _executed(env, 100)
    assert env.approver.submit(b'{"app":"example","verb":"restart","reason":"x"}')["ok"] is False
    assert env.slack.buttons() == []


def test_rate_limit_rechecked_at_click_time(env):
    pid = env.propose()
    _executed(env, 10)
    click(env, pid)
    assert env.executor.calls == [] and env.events()[-1] == "denied_approval"


# SEC-6: tope de propuestas pendientes
def _submit(env, app="example", verb="restart"):
    line = json.dumps({"app": app, "verb": verb, "reason": "x"}).encode()
    return env.approver.submit(line)


def test_pending_cap_per_app_rejects_without_posting(env):
    assert _submit(env)["ok"] and _submit(env)["ok"]
    posted = len(env.slack.posted)
    reply = _submit(env)
    assert reply["ok"] is False and "demasiadas" in reply["error"]
    assert len(env.slack.posted) == posted
    assert env.events()[-1] == "rejected_proposal"
    assert "demasiadas propuestas pendientes" in env.audit._path.read_text().splitlines()[-1]


def test_pending_cap_total(env):
    env.approver._pending.update({f"p{i}": _fake_pending(env, f"app{i}") for i in range(10)})
    reply = _submit(env)
    assert reply["ok"] is False and env.slack.posted == []


def _fake_pending(env, app):
    from rescue_bot.approver.proposals import Proposal

    now = env.clock.now
    return Proposal("x", app, "restart", "r", CHANNEL, now, now + 600, "d")


def test_expired_proposals_are_purged_on_propose(env):
    _submit(env)
    _submit(env)
    env.clock.now += 10_000
    assert _submit(env)["ok"] is True
    assert len(env.approver._pending) == 1


def test_expired_proposals_are_purged_on_click(env):
    pid = env.propose()
    env.clock.now += 10_000
    click(env, "otra")
    assert env.approver._pending == {}
    click(env, pid)
    assert env.executor.calls == []


# SEC-8: "executing" cuenta aunque no llegue "executed"
def test_executing_entry_written_before_action_and_counts(env):
    seen = []
    env.executor.gate = None
    real = env.executor.__call__

    class Spy:
        def __call__(self, app, verb):
            seen.append(env.events())
            return real(app, verb)

    env.approver._execute = Spy()
    pid = env.propose()
    click(env, pid)
    assert seen == [["proposed", "executing"]]
    assert env.events() == ["proposed", "executing", "executed"]
    assert len(env.audit.executed_epochs("example")) == 1  # deduplicado por propuesta


def test_crash_mid_execution_still_counts_after_restart(tmp_path):
    env = Env(tmp_path)

    class Boom(BaseException):
        pass

    def die(app, verb):
        raise Boom

    env.approver._execute = die
    pid = env.propose()
    with pytest.raises(Boom):
        click(env, pid)
    assert env.events() == ["proposed", "executing"]
    from rescue_bot.approver.ratelimit import RateLimiter

    fresh = RateLimiter(env.audit, env.approver._settings.limits, env.clock)
    assert fresh.check("example") is not None


# Slack: escapado
def test_reason_is_escaped_and_not_linkable(env):
    reason = "<!channel> <http://evil|ok> & `x` ```boom``` https://evil.example"
    env.approver.submit(
        json.dumps({"app": "example", "verb": "restart", "reason": reason}).encode()
    )
    text = env.slack.posted[-1]["blocks"][0]["text"]["text"]
    shown = text.split("Motivo (del agente): ", 1)[1]
    assert shown.startswith("`") and shown.endswith("`")
    assert "<" not in shown and ">" not in shown and "`" not in shown[1:-1]
    assert "&lt;!channel&gt;" in shown and "&amp;" in shown


def test_reason_display_is_capped(env):
    env.approver.submit(
        json.dumps({"app": "example", "verb": "restart", "reason": "a" * 500}).encode()
    )
    shown = env.slack.posted[-1]["blocks"][0]["text"]["text"]
    assert "a" * 201 not in shown and "a" * 200 in shown


def test_output_is_escaped_in_result(tmp_path):
    from rescue_bot.approver.executor import Outcome

    env = Env(tmp_path)
    env.approver._execute = lambda app, verb: Outcome(
        0, "<!here> a & b ``` <@U1>", True, "HTTP 200"
    )
    click(env, env.propose())
    text = env.slack.posted[-1]["text"]
    body = text.split("```", 1)[1].rsplit("```", 1)[0]
    assert "<" not in body and ">" not in body and "`" not in body
    assert "&lt;!here&gt; a &amp; b" in body


# Config: la huella es la de la carga
def test_proposals_bind_to_loaded_digest(env):
    pid = env.propose()
    assert env.approver._pending[pid].config_digest == env.approver._loaded_digest
