import json
import socket

import pytest
from test_approver_support import Env

from rescue_bot.approver.server import ProposalServer

LINE = b'{"app":"example","verb":"restart","reason":"cae"}\n'


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def talk(env, payload: bytes, peer_uid=1000, agent_uid=1000):
    server = ProposalServer(env.approver, env.audit, agent_uid, peer_uid=lambda _c: peer_uid)
    a, b = socket.socketpair(socket.AF_UNIX)
    a.sendall(payload)
    server.handle(b)
    reply = json.loads(a.makefile("rb").readline())
    a.close()
    return reply


def test_valid_proposal_over_socket(env):
    assert talk(env, LINE)["ok"] is True
    assert len(env.slack.buttons()) == 2


def test_wrong_peer_uid_is_rejected_without_posting(env):
    assert talk(env, LINE, peer_uid=0)["ok"] is False
    assert env.slack.posted == [] and env.events() == ["rejected_proposal"]


def test_oversized_line_is_rejected_without_posting(env):
    assert talk(env, b"x" * 6000 + b"\n")["ok"] is False
    assert env.slack.posted == [] and env.events() == ["rejected_proposal"]


def test_invalid_verb_over_socket(env):
    reply = talk(env, b'{"app":"example","verb":"rm","reason":"x"}\n')
    assert reply["ok"] is False and env.slack.posted == []
