"""Alta de una app en un solo comando: ficha, host key, enroll, Tailscale,
Healthchecks, commit de la configuración, recarga del CT y smoke test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from rescue_bot import health
from rescue_bot.config import App, ConfigError, known_hosts, load_app, load_settings
from rescue_bot.enroll import NAME_RE, EnrollError, Runner, enroll

HEALTHCHECKS_URL = "https://healthchecks.io/api/v3/checks/"
RELOAD_SCRIPT = "/usr/local/sbin/rescue-reload"
VERIFY_SCRIPT = "/usr/local/sbin/rescue-verify"
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


def render_ficha(name: str, values: Mapping[str, str | None]) -> str:
    if not NAME_RE.fullmatch(name):
        raise AddAppError(f"nombre de app inválido: {name!r}")
    preset = {"kind": values["preset"]}
    preset["unit" if values["preset"] == "systemd" else "dir"] = values["target"]
    for key in ("file", "trusted_owner", "trusted_group"):
        if values.get(key):
            preset[key] = values[key]
    owners = preset.get("trusted_owner", "").split(",")
    if len(owners) > 1:
        preset["trusted_owner"] = owners
    ficha = {
        "name": name,
        "url": values["url"],
        "health": {"url": values["health_url"], "expect": int(values["expect"] or 200)},
        "ssh": {"host": values["host"], "user": "rescue", "admin": values["admin"]},
        "preset": preset,
    }
    if values.get("rollback_cmd"):
        ficha["rollback_cmd"] = values["rollback_cmd"]
    return yaml.safe_dump(ficha, sort_keys=False)


def stage_config(config: Path, stage: Path, name: str, ficha: str) -> App:
    """Copia mínima de la config en `stage` con la ficha candidata; valida con load_app.

    Es lo que enroll ve: así ni la ficha ni known_hosts tocan la config real hasta que
    el alta termina."""
    (stage / "apps").mkdir()
    path = stage / "apps" / f"{name}.yaml"
    path.write_text(ficha, encoding="utf-8")
    app = load_app(path)
    shutil.copy(config / "settings.yaml", stage / "settings.yaml")
    if (config / "keys").is_dir():
        (stage / "keys").mkdir()
        for pub in (config / "keys").glob("*.pub"):
            shutil.copy(pub, stage / "keys" / pub.name)
    return app


def scan_host_key(config: Path, host: str, runner: Runner, console: Console) -> list[str]:
    """(b) ssh-keyscan → imprime huellas y devuelve las líneas nuevas para known_hosts.
    No escribe nada; rechaza una clave distinta de la ya conocida."""
    scan = _checked(runner.run(["ssh-keyscan", "-T", "5", host]), "ssh-keyscan")
    found = [line for line in scan.splitlines() if line and not line.startswith("#")]
    if not found:
        raise AddAppError(f"ssh-keyscan no devolvió claves de {host}")
    console.say(_checked(runner.run(["ssh-keygen", "-lf", "-"], input=scan), "ssh-keygen"))
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
    return fresh


def append_lines(path: Path, lines: Sequence[str]) -> None:
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.writelines(f"{line}\n" for line in lines)


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
    staged = runner.run([*git, "diff", "--cached", "--quiet"])
    if staged.returncode != 0:
        _checked(runner.run([*git, "commit", "-m", f"add app {name}"]), "git commit")
    _checked(runner.run([*git, "push"]), "git push")
    return True


def _ssh_ct(rescue_ssh: str, command: str, what: str, runner: Runner) -> None:
    if rescue_ssh.startswith("-"):
        raise AddAppError("rescue_host.ssh inválido")
    _checked(runner.run(["ssh", rescue_ssh, command]), what)


def reload_ct(rescue_ssh: str, runner: Runner) -> None:
    """(g) El CT hace pull con su deploy key y reinicia los servicios."""
    _ssh_ct(rescue_ssh, RELOAD_SCRIPT, "recarga del CT", runner)


def verify_in_ct(rescue_ssh: str, app: str, runner: Runner) -> None:
    """(h) La verificación corre en el CT: es el único sitio con la clave ro del agente."""
    _ssh_ct(rescue_ssh, f"{VERIFY_SCRIPT} {app}", "verificación en el CT", runner)


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
        "file": args.file,
        "trusted_owner": args.trusted_owner,
        "trusted_group": args.trusted_group,
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
    ficha = render_ficha(args.app, values)
    target = config / "apps" / f"{args.app}.yaml"
    if target.exists() and not args.update:
        raise AddAppError(f"{target} ya existe; usa --update para rehacer el alta")

    with tempfile.TemporaryDirectory(prefix="add-app-") as tmp:
        stage = Path(tmp)
        app = stage_config(config, stage, args.app, ficha)
        fresh = scan_host_key(config, app.host, runner, console)
        hosts = known_hosts(config)
        stage_hosts = known_hosts(stage)
        stage_hosts.write_text(hosts.read_text() if hosts.exists() else "", encoding="utf-8")
        append_lines(stage_hosts, fresh)

        if args.dry_run:
            console.say(f"--- {target} (no se escribe) ---\n{ficha}")
            enroll_fn(app, stage, dry_run=True, runner=runner, say=console.say)
            console.say("dry-run: nada escrito, aplicado ni subido")
            return

        if not console.confirm(f"¿Son las huellas de {app.host}?"):
            raise AddAppError("host key rechazada")
        enroll_fn(app, stage, dry_run=True, runner=runner, say=console.say)
        if not console.confirm(f"¿Aplicar el alta en {app.host}?"):
            raise AddAppError("cancelado")
        enroll_fn(app, stage, yes=True, runner=runner, say=console.say)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ficha, encoding="utf-8")
        append_lines(hosts, fresh)
    console.say(f"ficha escrita: {target}")

    console.say(check_tailscale_tag(app.host, runner))

    api_key = env.get("HC_API_KEY")
    if api_key and not args.no_healthcheck:
        console.say(f"Healthchecks: ping URL {ensure_healthcheck(app.name, api_key, runner)}")

    if not commit_config(config, app.name, runner):
        console.say("aviso: la configuración no es un repo git; no se hizo commit ni push")

    if not args.no_reload:
        reload_ct(settings.rescue_ssh, runner)
        console.say("CT recargado")
        verify_in_ct(settings.rescue_ssh, app.name, runner)
        console.say("verificación en el CT: ok")

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
    p.add_argument("--file", help="docker-compose: fichero compose no estándar dentro de --dir")
    p.add_argument(
        "--trusted-owner",
        help="docker-compose: usuarios (ya equivalentes a root), separados por comas, "
        "admitidos como dueños del proyecto",
    )
    p.add_argument(
        "--trusted-group",
        help="docker-compose: grupo con escritura admitida; "
        "todos sus miembros deben ser dueños de confianza",
    )
    p.add_argument("--health-url")
    p.add_argument("--expect", type=int, default=200)
    p.add_argument("--url")
    p.add_argument("--rollback-cmd")
    p.add_argument("--admin", default="root")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="sin efectos: ficha, huellas y diff")
    p.add_argument("--update", action="store_true", help="rehacer el alta de una app existente")
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
