"""Carga de la configuración privada: fichas de apps y ajustes del bot.

Estructura del directorio (ver examples/config/):
    settings.yaml       canal, aprobadores, límites, host del CT rescue
    known_hosts         host keys verificadas de las VMs (lo escribe add-app)
    apps/<app>.yaml     una ficha por app
    runbooks/<app>.md   opcional, fallos conocidos

En el CT es propiedad de root y de solo lectura para `agent` y `approver`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from rescue_bot.presets import Action, actions_for


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class App:
    name: str
    url: str
    health_url: str
    health_expect: int
    host: str
    user: str
    admin: str
    actions: tuple[Action, ...]
    runbook: Path | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def mode_of(self, verb: str) -> str | None:
        return next((a.mode for a in self.actions if a.verb == verb), None)

    def verbs(self, mode: str | None = None) -> list[str]:
        return [a.verb for a in self.actions if mode is None or a.mode == mode]


@dataclass(frozen=True)
class Limits:
    min_interval_s: int = 300
    max_per_hour: int = 3
    approval_ttl_s: int = 600


@dataclass(frozen=True)
class Settings:
    channel: str
    approvers: frozenset[str]
    rescue_ip: str
    rescue_ssh: str
    limits: Limits


def load_app(path: Path) -> App:
    raw = _read_yaml(path)
    name = raw.get("name") or path.stem
    if name != path.stem:
        raise ConfigError(f"{path}: `name` ({name}) no coincide con el fichero")
    try:
        ssh = raw["ssh"]
        health = raw["health"]
        actions = tuple(actions_for(raw))
        runbook = raw.get("runbook")
        return App(
            name=name,
            url=raw.get("url", health["url"]),
            health_url=health["url"],
            health_expect=int(health.get("expect", 200)),
            host=ssh["host"],
            user=ssh.get("user", "rescue"),
            admin=ssh.get("admin", "root"),
            actions=actions,
            runbook=(path.parent.parent / runbook) if runbook else None,
            raw=raw,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def load_apps(config_dir: Path) -> dict[str, App]:
    apps = {}
    for path in sorted((Path(config_dir) / "apps").glob("*.yaml")):
        app = load_app(path)
        apps[app.name] = app
    return apps


def load_settings(config_dir: Path) -> Settings:
    path = Path(config_dir) / "settings.yaml"
    raw = _read_yaml(path)
    try:
        slack = raw["slack"]
        rescue = raw["rescue_host"]
        return Settings(
            channel=slack["channel"],
            approvers=frozenset(slack.get("approvers") or []),
            rescue_ip=rescue["tailscale_ip"],
            rescue_ssh=rescue["ssh"],
            limits=Limits(**(raw.get("limits") or {})),
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def known_hosts(config_dir: Path) -> Path:
    return Path(config_dir) / "known_hosts"


def config_digest(config_dir: Path) -> str:
    """Huella de lo que decide qué se puede aprobar; una propuesta queda ligada a ella."""
    h = hashlib.sha256()
    root = Path(config_dir)
    apps = sorted((root / "apps").glob("*.yaml"))
    for path in [root / "settings.yaml", known_hosts(root), *apps]:
        if path.exists():
            h.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"no existe {path}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: se esperaba un mapa YAML")
    return data
