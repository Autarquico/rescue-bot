"""Protocolo del socket: una línea JSON `{app, verb, reason}` del agente al approver."""

from __future__ import annotations

import json
from dataclasses import dataclass

from rescue_bot.config import App

MAX_LINE = 4096
MAX_REASON = 500
KEYS = frozenset({"app", "verb", "reason"})


class ProtocolError(ValueError):
    """Petición inválida; el mensaje es seguro para devolver al agente."""


@dataclass(frozen=True)
class Request:
    app: str
    verb: str
    reason: str


def parse_request(line: bytes, apps: dict[str, App]) -> Request:
    if len(line) > MAX_LINE:
        raise ProtocolError("línea demasiado larga")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError("JSON inválido") from e
    if not isinstance(data, dict) or set(data) != KEYS:
        raise ProtocolError("se esperan exactamente las claves app, verb y reason")
    app, verb, reason = data["app"], data["verb"], data["reason"]
    if not all(isinstance(v, str) for v in (app, verb, reason)):
        raise ProtocolError("app, verb y reason deben ser texto")
    if len(reason) > MAX_REASON:
        raise ProtocolError(f"reason supera {MAX_REASON} caracteres")
    target = apps.get(app)
    if target is None:
        raise ProtocolError("app desconocida")
    mode = target.mode_of(verb)
    if mode is None:
        raise ProtocolError("verbo desconocido para esa app")
    if mode != "rw":
        raise ProtocolError("solo se proponen verbos de escritura (rw)")
    return Request(app, verb, reason)
