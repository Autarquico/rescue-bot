"""Alta de una app en un solo comando: ficha, host key, enroll, Tailscale,
Healthchecks, commit de la configuración, recarga del CT y smoke test."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from rescue_bot import health
from rescue_bot.config import App, ConfigError, known_hosts, load_app, load_settings
from rescue_bot.enroll import NAME_RE, EnrollError, Runner, enroll

HEALTHCHECKS_URL = "https://healthchecks.io/api/v3/checks/"
RELOAD_CMD = (
    "git -C /etc/rescue-bot/config pull --ff-only"
    " && systemctl restart rescue-approver hermes-gateway"
)
TAG_COMMAND = "tailscale set --advertise-tags=tag:app"


class AddAppError(RuntimeError):
    pass


class Console:
    def __init__(self, yes: bool = False):
        self.yes = yes

    def say(self, text: str) -> None:
        print(text)

    def ask(self, question: str, default: str | None = None) -> str:
        if self.yes:
            if default is None:
                raise AddAppError(f"falta un valor: {question}")
            return default
        suffix = f" [{default}]" if default else ""
        return input(f"{question}{suffix}: ").strip() or (default or "")

    def confirm(self, question: str) -> bool:
        return self.yes or input(f"{question} [s/N] ").strip().lower() in ("s", "y")


def _checked(proc, what: str) -> str:
    if proc.returncode != 0:
        raise AddAppError(f"{what} falló: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def write_ficha(config: Path, name: str, values: Mapping[str, str | None]) -> Path:
    """(a) Escribe apps/<name>.yaml y lo valida con load_app; si no vale, lo borra."""
    if not NAME_RE.fullmatch(name):
        raise AddAppError(f"nombre de app inválido: {name!r}")
    path = config / "apps" / f"{name}.yaml"
    if path.exists():
        raise AddAppError(f"{path} ya existe; bórralo si quieres rehacer el alta")
    preset = {"kind": values["preset"]}
    preset["unit" if values["preset"] == "systemd" else "dir"] = values["target"]
    ficha = {
        "name": name,
        "url": values["url"],
        "health": {"url": values["health_url"], "expect": int(values["expect"] or 200)},
        "ssh": {"host": values["host"], "user": "rescue", "admin": values["admin"]},
        "preset": preset,
    }
    if values.get("rollback_cmd"):
        ficha["rollback_cmd"] = values["rollback_cmd"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(ficha, sort_keys=False), encoding="utf-8")
    try:
        load_app(path)
    except ConfigError:
        path.unlink()
        raise
    return path


def record_host_key(config: Path, host: str, runner: Runner, console: Console) -> None:
    """(b) ssh-keyscan → known_hosts, sin duplicar y sin aceptar una clave distinta."""
    scan = _checked(runner.run(["ssh-keyscan", "-T", "5", host]), "ssh-keyscan")
    found = [line for line in scan.splitlines() if line and not line.startswith("#")]
    if not found:
        raise AddAppError(f"ssh-keyscan no devolvió claves de {host}")
    console.say(_checked(runner.run(["ssh-keygen", "-lf", "-"], input=scan), "ssh-keygen"))
    if not console.confirm(f"¿Son las huellas de {host}?"):
        raise AddAppError("host key rechazada")
    path = known_hosts(config)
    existing = path.read_text().splitlines() if path.exists() else []
    seen = {tuple(line.split()[:3]) for line in existing if line.strip()}
    slots = {tuple(line.split()[:2]) for line in existing if line.strip()}
    fresh = []
    for line in found:
        entry = tuple(line.split()[:3])
        if entry in seen:
            continue
        if entry[:2] in slots:
            raise AddAppError(f"la host key de {host} ({entry[1]}) cambió; resuélvelo a mano")
        fresh.append(line)
    if fresh:
        with path.open("a", encoding="utf-8") as f:
            f.writelines(f"{line}\n" for line in fresh)


def check_tailscale_tag(host: str, runner: Runner) -> str:
    """(d) Mensaje de estado; nunca falla el alta."""
    if not runner.which("tailscale"):
        return f"aviso: sin CLI de tailscale; comprueba el tag en {host} (`{TAG_COMMAND}`)"
    proc = runner.run(["tailscale", "status", "--json"])
    if proc.returncode != 0:
        return f"aviso: tailscale status falló; comprueba el tag (`{TAG_COMMAND}`)"
    short = host.split(".")[0]
    for peer in (json.loads(proc.stdout).get("Peer") or {}).values():
        dns = (peer.get("DNSName") or "").rstrip(".")
        if host in (dns, peer.get("HostName")) or dns.split(".")[0] == short:
            if "tag:app" in (peer.get("Tags") or []):
                return f"tailscale: {host} tiene tag:app"
            return f"aviso: {host} sin tag:app; en la VM ejecuta `{TAG_COMMAND}`"
    return f"aviso: {host} no aparece en tailscale; en la VM ejecuta `{TAG_COMMAND}`"


def ensure_healthcheck(name: str, api_key: str, runner: Runner) -> str:
    """(e) Crea o actualiza el check `<app>-up` (unique por nombre) y devuelve su ping URL."""
    body = {"name": f"{name}-up", "unique": ["name"], "channels": "*"}
    return runner.post_json(HEALTHCHECKS_URL, {"X-Api-Key": api_key}, body)["ping_url"]


def commit_config(config: Path, name: str, runner: Runner) -> bool:
    """(f) add + commit + push de lo que toca el alta. False si no es un repo git."""
    if not (config / ".git").exists():
        return False
    git = ["git", "-C", str(config)]
    _checked(runner.run([*git, "add", f"apps/{name}.yaml", "known_hosts"]), "git add")
    _checked(runner.run([*git, "commit", "-m", f"add app {name}"]), "git commit")
    _checked(runner.run([*git, "push"]), "git push")
    return True


def reload_ct(rescue_ssh: str, runner: Runner) -> None:
    """(g) El CT hace pull de la configuración y reinicia los servicios."""
    if rescue_ssh.startswith("-"):
        raise AddAppError("rescue_host.ssh inválido")
    _checked(runner.run(["ssh", rescue_ssh, RELOAD_CMD]), "recarga del CT")


def _collect(args: argparse.Namespace, console: Console) -> dict[str, str | None]:
    host = args.host or console.ask("Host (MagicDNS de Tailscale)")
    preset = args.preset or console.ask("Preset (systemd|docker-compose)")
    if preset == "systemd":
        target = args.unit or console.ask("Unidad systemd")
    elif preset == "docker-compose":
        target = args.dir or console.ask("Directorio del proyecto compose (ruta absoluta)")
    else:
        raise AddAppError(f"preset desconocido: {preset!r}")
    health_url = args.health_url or console.ask("URL de salud")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(health_url))
    return {
        "host": host,
        "preset": preset,
        "target": target,
        "health_url": health_url,
        "expect": str(args.expect),
        "url": args.url or console.ask("URL pública", origin),
        "admin": args.admin,
        "rollback_cmd": args.rollback_cmd,
    }


def add_app(
    args: argparse.Namespace,
    *,
    runner: Runner | None = None,
    console: Console | None = None,
    env: Mapping[str, str] | None = None,
    enroll_fn: Callable[..., None] = enroll,
    check_health: Callable[[App], health.Health] = health.check,
) -> None:
    runner = runner or Runner()
    console = console or Console(args.yes)
    env = os.environ if env is None else env
    config: Path = args.config
    settings = load_settings(config)

    values = _collect(args, console)
    ficha = write_ficha(config, args.app, values)
    app = load_app(ficha)
    console.say(f"ficha escrita: {ficha}")

    record_host_key(config, app.host, runner, console)

    enroll_fn(app, config, dry_run=True, runner=runner, say=console.say)
    if not console.confirm(f"¿Aplicar el alta en {app.host}?"):
        raise AddAppError("cancelado")
    enroll_fn(app, config, yes=True, runner=runner, say=console.say)

    console.say(check_tailscale_tag(app.host, runner))

    api_key = env.get("HC_API_KEY")
    if api_key and not args.no_healthcheck:
        console.say(f"Healthchecks: ping URL {ensure_healthcheck(app.name, api_key, runner)}")

    if not commit_config(config, app.name, runner):
        console.say("aviso: la configuración no es un repo git; no se hizo commit ni push")

    if not args.no_reload:
        reload_ct(settings.rescue_ssh, runner)
        console.say("CT recargado")

    result = check_health(app)
    console.say(f"smoke: {'ok' if result.ok else 'FALLA'} ({result.detail})")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="add-app", description=__doc__)
    p.add_argument("app")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--host")
    p.add_argument("--preset", choices=["systemd", "docker-compose"])
    p.add_argument("--unit")
    p.add_argument("--dir")
    p.add_argument("--health-url")
    p.add_argument("--expect", type=int, default=200)
    p.add_argument("--url")
    p.add_argument("--rollback-cmd")
    p.add_argument("--admin", default="root")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-reload", action="store_true")
    p.add_argument("--no-healthcheck", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    try:
        add_app(parser().parse_args(argv))
    except (AddAppError, EnrollError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
