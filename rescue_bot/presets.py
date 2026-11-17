"""Catálogo de acciones de una VM: de la ficha de la app a verbos con modo y argv.

Es la única fuente de verdad del catálogo. El bot solo usa `verb` y `mode`; el
argv solo viaja a la VM, escrito por `enroll` en /etc/rescue/actions.conf.

Ningún comando pasa por un shell: cada acción es un argv cuyos elementos usan un
juego de caracteres que no necesita escape ni en actions.conf (separado por
espacios) ni en sudoers (sin comodines, comas, dos puntos, `=` ni barras inversas).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VERB_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ARG_RE = re.compile(r"[A-Za-z0-9@%+./_-]+")
ABS_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")
UNIT_RE = re.compile(r"[A-Za-z0-9@._-]+")
MODES = ("ro", "rw")
RESOURCES = ("/usr/lib/rescue/resources.sh",)


class PresetError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    verb: str
    mode: str
    argv: tuple[str, ...]
    sudo: bool = False


def is_verb(value: object) -> bool:
    return isinstance(value, str) and VERB_RE.fullmatch(value) is not None


def _systemd(params: dict) -> list[Action]:
    unit = _param(params, "unit", UNIT_RE)
    return [
        Action("status", "ro", ("/usr/bin/systemctl", "status", "--no-pager", unit)),
        Action("logs", "ro", ("/usr/bin/journalctl", "-u", unit, "-n", "200", "--no-pager")),
        Action("resources", "ro", RESOURCES),
        Action("restart", "rw", ("/usr/bin/systemctl", "restart", unit), sudo=True),
    ]


def _docker_compose(params: dict) -> list[Action]:
    compose = (
        "/usr/bin/docker",
        "compose",
        "--project-directory",
        _param(params, "dir", ABS_PATH_RE),
    )
    return [
        Action("status", "ro", (*compose, "ps"), sudo=True),
        Action("logs", "ro", (*compose, "logs", "--no-color", "--tail", "200"), sudo=True),
        Action("resources", "ro", RESOURCES),
        Action("restart", "rw", (*compose, "restart"), sudo=True),
    ]


PRESETS = {"systemd": _systemd, "docker-compose": _docker_compose}


def _param(params: dict, key: str, pattern: re.Pattern) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PresetError(f"el preset necesita `{key}` válido (patrón {pattern.pattern})")
    return value


def actions_for(app: dict) -> list[Action]:
    """Catálogo de una ficha: preset + `rollback_cmd` opcional + `vm_actions` propios."""
    if "actions" in app:
        raise PresetError(
            "`actions` no se declara a mano: usa `preset`, `rollback_cmd` o `vm_actions`"
        )
    actions: list[Action] = []
    preset = app.get("preset")
    if preset:
        kind = preset.get("kind")
        if kind not in PRESETS:
            raise PresetError(f"preset desconocido: {kind!r} (válidos: {', '.join(PRESETS)})")
        actions += PRESETS[kind](preset)
    if app.get("rollback_cmd"):
        path = app["rollback_cmd"]
        if not isinstance(path, str) or not ABS_PATH_RE.fullmatch(path):
            raise PresetError("`rollback_cmd` debe ser una ruta absoluta sin argumentos")
        actions.append(Action("rollback", "rw", (path,), sudo=True))
    for raw in app.get("vm_actions") or []:
        argv = raw.get("command")
        if not isinstance(argv, list) or not argv:
            raise PresetError("`vm_actions[].command` debe ser una lista argv")
        actions.append(Action(raw.get("verb"), raw.get("mode"), tuple(argv), bool(raw.get("sudo"))))
    if not actions:
        raise PresetError("la ficha no declara ninguna acción")
    _validate(actions)
    return actions


def _validate(actions: list[Action]) -> None:
    seen: set[str] = set()
    for a in actions:
        if not is_verb(a.verb):
            raise PresetError(f"verbo inválido: {a.verb!r}")
        if a.mode not in MODES:
            raise PresetError(f"modo inválido para {a.verb}: {a.mode!r}")
        if not ABS_PATH_RE.fullmatch(a.argv[0]):
            raise PresetError(f"{a.verb}: el ejecutable debe ser una ruta absoluta")
        bad = [x for x in a.argv if not isinstance(x, str) or not ARG_RE.fullmatch(x)]
        if bad:
            raise PresetError(f"{a.verb}: argumentos no permitidos {bad!r}")
        if a.verb in seen:
            raise PresetError(f"verbo duplicado: {a.verb}")
        seen.add(a.verb)
