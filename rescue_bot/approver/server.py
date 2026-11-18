"""Servicio approver: socket Unix para el agente + app de Slack (Socket Mode) para los botones."""

from __future__ import annotations

import json
import logging
import os
import pwd
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from rescue_bot import config
from rescue_bot.approver.audit import AuditLog
from rescue_bot.approver.executor import Executor
from rescue_bot.approver.proposals import APPROVE_ID, REJECT_ID, Approver
from rescue_bot.approver.protocol import MAX_LINE
from rescue_bot.approver.ratelimit import RateLimiter

log = logging.getLogger(__name__)
CLIENT_TIMEOUT_S = 5


def linux_peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _read_line(conn: socket.socket) -> bytes | None:
    """Una línea sin el salto final, o None si excede MAX_LINE."""
    data = b""
    while b"\n" not in data and len(data) <= MAX_LINE:
        chunk = conn.recv(MAX_LINE + 1)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\n", 1)[0]
    return line if len(line) <= MAX_LINE else None


class ProposalServer:
    def __init__(
        self,
        approver: Approver,
        audit: AuditLog,
        agent_uid: int,
        peer_uid: Callable[[socket.socket], int] = linux_peer_uid,
    ):
        self._approver = approver
        self._audit = audit
        self._agent_uid = agent_uid
        self._peer_uid = peer_uid

    def handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(CLIENT_TIMEOUT_S)
            try:
                reply = self._reply(conn)
                conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            except OSError:
                log.warning("conexión del agente interrumpida")

    def _reply(self, conn: socket.socket) -> dict:
        if self._peer_uid(conn) != self._agent_uid:
            self._audit.write("rejected_proposal", detail="peer uid no autorizado")
            return {"ok": False, "error": "no autorizado"}
        line = _read_line(conn)
        if line is None:
            self._audit.write("rejected_proposal", detail="línea demasiado larga")
            return {"ok": False, "error": "línea demasiado larga"}
        return self._approver.submit(line)

    def serve(self, path: Path) -> socket.socket:
        """Crea el socket (0660) y atiende en un hilo; devuelve el socket de escucha."""
        path = Path(path)
        path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o660)
        srv.listen(8)
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()
        return srv

    def _accept_loop(self, srv: socket.socket) -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()


def build_approver(slack, env: dict[str, str] | os._Environ) -> tuple[Approver, AuditLog]:
    config_dir = Path(env["RESCUE_CONFIG"])
    # Huella primero y configuración después: si algo cambia entre ambas, la huella queda
    # vieja y las aprobaciones se deniegan (falla hacia el lado seguro).
    loaded_digest = config.config_digest(config_dir)
    settings = config.load_settings(config_dir)
    apps = config.load_apps(config_dir)
    audit = AuditLog(Path(env["RESCUE_AUDIT"]))
    approver = Approver(
        apps=apps,
        settings=settings,
        slack=slack,
        audit=audit,
        limiter=RateLimiter(audit, settings.limits),
        execute=Executor(Path(env["RESCUE_KEY_RW"]), config.known_hosts(config_dir)),
        loaded_digest=loaded_digest,
        digest=lambda: config.config_digest(config_dir),
    )
    return approver, audit


def main() -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    logging.basicConfig(level=logging.INFO)
    env = os.environ
    slack_app = App(token=env["SLACK_BOT_TOKEN"])
    approver, audit = build_approver(slack_app.client, env)
    agent_uid = pwd.getpwnam(env["RESCUE_AGENT_USER"]).pw_uid
    server = ProposalServer(approver, audit, agent_uid)
    socket_path = Path(env["RESCUE_SOCKET"])
    server.serve(socket_path)

    def on_click(approve: bool):
        def handler(ack, body):
            ack()
            approver.decide(
                body["actions"][0]["value"],
                body["user"]["id"],
                (body.get("channel") or {}).get("id", ""),
                (body.get("message") or {}).get("ts", ""),
                approve,
            )

        return handler

    slack_app.action(APPROVE_ID)(on_click(True))
    slack_app.action(REJECT_ID)(on_click(False))
    SocketModeHandler(slack_app, env["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
