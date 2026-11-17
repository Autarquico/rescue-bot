"""Propuestas pendientes y decisión humana.

Las propuestas viven solo en memoria: tras un reinicio ninguna es conocida y todo
clic sobre ellas se rechaza. La decisión (comprobaciones, consumo y reserva del
límite) ocurre bajo un único cerrojo; la ejecución, fuera de él, pero ya consumida.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rescue_bot.approver.audit import AuditLog
from rescue_bot.approver.executor import Outcome
from rescue_bot.approver.protocol import ProtocolError, Request, parse_request
from rescue_bot.approver.ratelimit import RateLimiter
from rescue_bot.config import App, Settings

log = logging.getLogger(__name__)

APPROVE_ID = "rescue_approve"
REJECT_ID = "rescue_reject"
MAX_OUTPUT = 3000


@dataclass(frozen=True)
class Proposal:
    id: str
    app: str
    verb: str
    reason: str
    channel: str
    created: float
    expires: float
    config_digest: str


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def proposal_blocks(p: Proposal) -> list[dict]:
    text = f"¿Ejecutar *{_esc(p.verb)}* en *{_esc(p.app)}*?\nMotivo (del agente): {_esc(p.reason)}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Aprobar"},
                    "action_id": APPROVE_ID,
                    "value": p.id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Rechazar"},
                    "action_id": REJECT_ID,
                    "value": p.id,
                },
            ],
        },
    ]


class Approver:
    def __init__(
        self,
        *,
        apps: dict[str, App],
        settings: Settings,
        slack: Any,
        audit: AuditLog,
        limiter: RateLimiter,
        execute: Callable[[App, str], Outcome],
        digest: Callable[[], str],
        clock: Callable[[], float] = time.time,
    ):
        self._apps = apps
        self._settings = settings
        self._slack = slack
        self._audit = audit
        self._limiter = limiter
        self._execute = execute
        self._digest = digest
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, Proposal] = {}

    # --- propuestas del agente -------------------------------------------------

    def submit(self, line: bytes) -> dict:
        try:
            req = parse_request(line, self._apps)
        except ProtocolError as e:
            self._audit.write("rejected_proposal", detail=str(e))
            return {"ok": False, "error": str(e)}
        return self._propose(req)

    def _propose(self, req: Request) -> dict:
        channel = self._settings.channel
        blocked = self._limiter.check(req.app)
        if blocked:
            self._audit.write("blocked_proposal", app=req.app, verb=req.verb, detail=blocked)
            self._say(
                channel,
                f"Límite alcanzado en *{_esc(req.app)}* ({blocked}): intervención manual.",
            )
            return {"ok": False, "error": f"límite alcanzado: {blocked}; intervención manual"}
        now = self._clock()
        p = Proposal(
            id=secrets.token_urlsafe(16),
            app=req.app,
            verb=req.verb,
            reason=req.reason,
            channel=channel,
            created=now,
            expires=now + self._settings.limits.approval_ttl_s,
            config_digest=self._digest(),
        )
        with self._lock:
            self._pending[p.id] = p
        try:
            self._slack.chat_postMessage(
                channel=channel,
                text=f"¿Ejecutar {p.verb} en {p.app}?",
                blocks=proposal_blocks(p),
            )
        except Exception:
            log.exception("no se pudo publicar la propuesta")
            with self._lock:
                self._pending.pop(p.id, None)
            return {"ok": False, "error": "no se pudo publicar en Slack"}
        self._audit.write(
            "proposed", app=p.app, verb=p.verb, proposal=p.id, detail={"reason": p.reason}
        )
        return {"ok": True, "message": "propuesta publicada; requiere aprobación humana en Slack"}

    # --- clic humano -----------------------------------------------------------

    def decide(self, proposal_id: str, user: str, channel: str, ts: str, approve: bool) -> None:
        with self._lock:
            verdict, p, why = self._verdict(proposal_id, user, channel, approve)
        if verdict == "deny":
            self._deny(p, proposal_id, user, channel, why)
        elif verdict == "decline" and p:
            self._audit.write("declined", app=p.app, verb=p.verb, proposal=p.id, user=user)
            self._update(channel, ts, f"Rechazada por <@{user}>: {_esc(p.verb)} en {_esc(p.app)}.")
        elif verdict == "run" and p:
            self._run(p, user, channel, ts)

    def _verdict(self, pid: str, user: str, channel: str, approve: bool):
        """Bajo el cerrojo. Devuelve (veredicto, propuesta, motivo de la denegación)."""
        p = self._pending.get(pid)
        if p is None:
            return "deny", None, "propuesta desconocida, ya usada o caducada"
        if channel != p.channel:
            return "deny", p, "canal distinto al de la propuesta"
        if user not in self._settings.approvers:
            return "deny", p, "usuario no autorizado"
        if not approve:
            del self._pending[pid]
            return "decline", p, None
        if self._clock() >= p.expires:
            reason = "propuesta caducada"
        elif self._digest() != p.config_digest:
            reason = "la configuración cambió desde la propuesta"
        else:
            reason = self._limiter.reserve(p.app)
        if reason:
            del self._pending[pid]
            return "deny", p, reason
        del self._pending[pid]
        return "run", p, None

    def _deny(self, p: Proposal | None, pid: str, user: str, channel: str, reason: str) -> None:
        self._audit.write(
            "denied_approval",
            app=p.app if p else None,
            verb=p.verb if p else None,
            proposal=pid if p else None,
            user=user,
            detail=reason,
        )
        try:
            self._slack.chat_postEphemeral(
                channel=channel, user=user, text=f"No se ejecuta nada: {reason}."
            )
        except Exception:
            log.exception("no se pudo avisar al usuario")

    def _run(self, p: Proposal, user: str, channel: str, ts: str) -> None:
        self._update(
            channel, ts, f"Aprobada por <@{user}>: ejecutando {_esc(p.verb)} en {_esc(p.app)}…"
        )
        try:
            outcome = self._execute(self._apps[p.app], p.verb)
        except Exception as e:
            log.exception("fallo ejecutando")
            outcome = Outcome(-1, f"error interno: {type(e).__name__}", None, "no comprobada")
        try:
            self._audit.write(
                "executed",
                app=p.app,
                verb=p.verb,
                proposal=p.id,
                user=user,
                detail={
                    "code": outcome.code,
                    "health_ok": outcome.health_ok,
                    "health": outcome.health_detail,
                },
            )
        finally:
            self._limiter.release(p.app)
        verdict = "OK" if outcome.code == 0 else f"falló (código {outcome.code})"
        state = {True: "sana", False: "NO sana", None: "sin comprobar"}[outcome.health_ok]
        self._update(
            channel, ts, f"{_esc(p.verb)} en {_esc(p.app)} aprobada por <@{user}>: {verdict}."
        )
        output = outcome.output[-MAX_OUTPUT:].replace("```", "'''")
        self._say(
            channel,
            f"Resultado de {_esc(p.verb)} en {_esc(p.app)}: {verdict}. "
            f"Salud: {state} ({_esc(outcome.health_detail)}).\n```{output}```",
            thread_ts=ts,
        )

    # --- Slack, sin propagar fallos --------------------------------------------

    def _say(self, channel: str, text: str, thread_ts: str | None = None) -> None:
        try:
            kwargs = {"thread_ts": thread_ts} if thread_ts else {}
            self._slack.chat_postMessage(channel=channel, text=text, **kwargs)
        except Exception:
            log.exception("no se pudo publicar en Slack")

    def _update(self, channel: str, ts: str, text: str) -> None:
        try:
            self._slack.chat_update(channel=channel, ts=ts, text=text, blocks=[])
        except Exception:
            log.exception("no se pudo actualizar el mensaje")
