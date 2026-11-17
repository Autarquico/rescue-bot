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
    assert env.events() == ["proposed", "executed"]


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
    assert env.events() == ["proposed", "executed", "denied_approval"]


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
    assert "manual" in env.slack.posted[-1]["text"]


def test_action_within_min_interval_is_blocked(env):
    _executed(env, 100)
    assert env.approver.submit(b'{"app":"example","verb":"restart","reason":"x"}')["ok"] is False
    assert env.slack.buttons() == []


def test_rate_limit_rechecked_at_click_time(env):
    pid = env.propose()
    _executed(env, 10)
    click(env, pid)
    assert env.executor.calls == [] and env.events()[-1] == "denied_approval"
