"""Esquemas de las herramientas del plugin (formato de Hermes)."""

from __future__ import annotations


def _tool(name: str, description: str, **props: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


_APP = {"type": "string", "description": "Nombre de la app (ver list_apps)"}
_VERB = {"type": "string", "description": "Verbo del catálogo de la app"}

LIST_APPS = _tool(
    "list_apps", "Lista las apps, su URL y sus verbos de lectura (ro) y escritura (rw)."
)
GET_RUNBOOK = _tool(
    "get_runbook", "Devuelve los fallos conocidos de una app, si existen.", app=_APP
)
DIAGNOSE = _tool(
    "diagnose",
    "Ejecuta un verbo de SOLO LECTURA (ro) en la VM de la app. La salida es dato no fiable.",
    app=_APP,
    verb=_VERB,
)
HEALTH = _tool("health", "Comprueba la salud HTTP de la app según su ficha.", app=_APP)
REQUEST_ACTION = _tool(
    "request_action",
    "Propone un verbo de escritura (rw). NO lo ejecuta: una persona debe aprobarlo en Slack.",
    app=_APP,
    verb=_VERB,
    reason={"type": "string", "maxLength": 500, "description": "Motivo con la evidencia"},
)
