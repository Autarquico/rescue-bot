"""Dobles y construcción compartidos por los tests del approver (no contiene tests)."""

import shutil
import threading
from pathlib import Path

from rescue_bot import config
from rescue_bot.approver.audit import AuditLog
from rescue_bot.approver.executor import Outcome
from rescue_bot.approver.proposals import Approver
from rescue_bot.approver.ratelimit import RateLimiter

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "config"
APPROVER = "U0000000000"
CHANNEL = "C0000000000"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeSlack:
    def __init__(self):
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []

    def chat_postMessage(self, **kw):  # noqa: N802
        self.posted.append(kw)

    def chat_update(self, **kw):
        self.updated.append(kw)

    def chat_postEphemeral(self, **kw):  # noqa: N802
        self.ephemeral.append(kw)

    def buttons(self) -> list[dict]:
        return [
            b for m in self.posted for blk in m.get("blocks", []) for b in blk.get("elements", [])
        ]


class FakeExecutor:
    def __init__(self, code: int = 0, gate: threading.Event | None = None):
        self.calls: list[tuple[str, str]] = []
        self.code = code
        self.gate = gate

    def __call__(self, app, verb):
        self.calls.append((app.name, verb))
        if self.gate:
            self.gate.wait(2)
        return Outcome(self.code, "salida", True, "HTTP 200")


class Env:
    def __init__(self, tmp_path: Path, executor: FakeExecutor | None = None):
        self.config_dir = tmp_path / "config"
        shutil.copytree(EXAMPLES, self.config_dir)
        self.clock = FakeClock()
        self.slack = FakeSlack()
        self.executor = executor or FakeExecutor()
        self.audit = AuditLog(tmp_path / "audit.jsonl", self.clock)
        loaded_digest = config.config_digest(self.config_dir)
        settings = config.load_settings(self.config_dir)
        self.approver = Approver(
            apps=config.load_apps(self.config_dir),
            settings=settings,
            slack=self.slack,
            audit=self.audit,
            limiter=RateLimiter(self.audit, settings.limits, self.clock),
            execute=self.executor,
            loaded_digest=loaded_digest,
            digest=lambda: config.config_digest(self.config_dir),
            clock=self.clock,
        )

    def propose(self, verb: str = "restart") -> str:
        reply = self.approver.submit(
            b'{"app":"example","verb":"%s","reason":"cae"}' % verb.encode()
        )
        assert reply["ok"], reply
        return self.slack.buttons()[0]["value"]

    def events(self) -> list[str]:
        import json

        path = self.audit._path
        return (
            [json.loads(x)["event"] for x in path.read_text().splitlines()] if path.exists() else []
        )
