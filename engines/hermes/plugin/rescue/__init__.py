"""Plugin `rescue` de Hermes: diagnóstico de solo lectura y propuestas de acción."""

from . import schemas, tools

_TOOLS = (
    ("list_apps", schemas.LIST_APPS, tools.list_apps),
    ("get_runbook", schemas.GET_RUNBOOK, tools.get_runbook),
    ("diagnose", schemas.DIAGNOSE, tools.diagnose),
    ("health", schemas.HEALTH, tools.health_tool),
    ("request_action", schemas.REQUEST_ACTION, tools.request_action),
)


def register(ctx) -> None:
    for name, schema, handler in _TOOLS:
        ctx.register_tool(name=name, toolset="rescue", schema=schema, handler=handler)
