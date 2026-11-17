"""Alta de una VM: instala rescue-shell, el catálogo, sudoers y las claves.

Lo ejecuta una persona con credenciales de administrador (ficha: `ssh.admin`).
Los renderizadores son funciones puras; `enroll` las aplica por SSH con un script
remoto idempotente que viaja por stdin (ningún fichero se copia con scp).
"""

from __future__ import annotations

import argparse
import base64
import difflib
import ipaddress
import json
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from rescue_bot.config import App, ConfigError, known_hosts, load_app, load_settings
from rescue_bot.presets import Action
from rescue_bot.ssh import Result, run_verb

VM_DIR = Path(__file__).resolve().parent.parent / "vm"
SHELL_PATH = "/usr/local/sbin/rescue-shell"
RESOURCES_PATH = "/usr/local/lib/rescue/resources.sh"
ACTIONS_PATH = "/etc/rescue/actions.conf"
SUDOERS_PATH = "/etc/sudoers.d/rescue"
KEYS_PATH = "/home/rescue/.ssh/authorized_keys"

NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]*")
PUBKEY_RE = re.compile(
    r"(ssh-[a-z0-9]+|ecdsa-sha2-[a-z0-9]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( [^\n\r\"]*)?"
)
COMPOSE_FILES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    ".env",
)


class EnrollError(RuntimeError):
    pass


class Runner:
    """Único punto de salida al mundo: procesos, búsqueda de ejecutables y HTTP."""

    def run(
        self, argv: Sequence[str], *, input: str | None = None, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            list(argv), input=input, capture_output=True, text=True, timeout=timeout, check=False
        )

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def post_json(self, url: str, headers: dict[str, str], body: dict) -> dict:
        req = urllib.request.Request(  # noqa: S310 (https fijo en el llamador)
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.load(resp)


# --- renderizadores puros -------------------------------------------------


def render_actions_conf(actions: Sequence[Action]) -> str:
    lines = ["# Generado por rescue-bot enroll. No editar a mano.", "# verbo modo [sudo] argv..."]
    for a in actions:
        lines.append(" ".join([a.verb, a.mode, *(["sudo"] if a.sudo else []), *a.argv]))
    return "\n".join(lines) + "\n"


def render_sudoers(app_name: str, actions: Sequence[Action]) -> str:
    if not NAME_RE.fullmatch(app_name):
        raise EnrollError(f"nombre de app inválido: {app_name!r}")
    lines = [f"# Generado por rescue-bot enroll para {app_name}. No editar a mano."]
    lines += [f"rescue ALL=(root) NOPASSWD: {' '.join(a.argv)}" for a in actions if a.sudo]
    return "\n".join(lines) + "\n"


def render_authorized_keys(rescue_ip: str, ro_pub: str, rw_pub: str) -> str:
    ip = str(ipaddress.ip_address(rescue_ip))
    lines = []
    for mode, pub in (("ro", ro_pub), ("rw", rw_pub)):
        pub = pub.strip()
        if not PUBKEY_RE.fullmatch(pub):
            raise EnrollError(f"clave pública {mode} inválida")
        options = f'restrict,from="{ip}",command="{SHELL_PATH} {mode}"'
        lines.append(f"{options} {pub}")
    return "\n".join(lines) + "\n"


# --- script remoto --------------------------------------------------------


def _b64(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return base64.b64encode(raw).decode()


def desired_files(app: App, config: Path) -> dict[str, tuple[str, str]]:
    """ruta remota → (contenido, modo). Es lo que enroll deja en la VM."""
    settings = load_settings(config)
    keys = config / "keys"
    try:
        ro_pub = (keys / "key_ro.pub").read_text()
        rw_pub = (keys / "key_rw.pub").read_text()
    except FileNotFoundError as e:
        raise EnrollError(f"falta {e.filename}") from e
    return {
        SHELL_PATH: ((VM_DIR / "rescue-shell").read_text(), "0755"),
        RESOURCES_PATH: ((VM_DIR / "resources.sh").read_text(), "0755"),
        ACTIONS_PATH: (render_actions_conf(app.actions), "0644"),
        SUDOERS_PATH: (render_sudoers(app.name, app.actions), "0440"),
        KEYS_PATH: (render_authorized_keys(settings.rescue_ip, ro_pub, rw_pub), "0644"),
    }


def _compose_guard(directory: str) -> str:
    files = " ".join(shlex.quote(f"{directory}/{name}") for name in COMPOSE_FILES)
    return f"""
for p in {shlex.quote(directory)} {files}; do
  [ -e "$p" ] || continue
  [ "$(stat -c %u "$p")" = 0 ] || die "$p no es de root"
  [ $(( 8#$(stat -c %a "$p") & 8#022 )) = 0 ] || die "$p escribible por grupo u otros"
done
"""


def remote_script(app: App, files: dict[str, tuple[str, str]]) -> str:
    kind = (app.raw.get("preset") or {}).get("kind")
    guard = _compose_guard(app.raw["preset"]["dir"]) if kind == "docker-compose" else ""

    def put(path: str, content: str, mode: str) -> str:
        return (
            f'printf %s {_b64(content)} | base64 -d > "$tmp"\n'
            f'install -o root -g root -m {mode} "$tmp" {path}\n'
        )

    sudoers_content, _ = files[SUDOERS_PATH]
    parts = [
        "set -euo pipefail\numask 022\n",
        'die() { echo "enroll: $*" >&2; exit 1; }\n',
        "tmp=$(mktemp); trap 'rm -f \"$tmp\"' EXIT\n",
        guard,
        "id rescue >/dev/null 2>&1 || useradd --system --home-dir /home/rescue "
        "--no-create-home --shell /bin/bash rescue\n",
        # `*` no es contraseña válida pero tampoco bloquea la cuenta (pubkey sigue funcionando).
        "usermod -p '*' rescue\n",
        "install -d -o root -g root -m 0755 /home/rescue /home/rescue/.ssh "
        "/usr/local/sbin /usr/local/lib/rescue /etc/rescue\n",
        put(SHELL_PATH, *files[SHELL_PATH]),
        put(RESOURCES_PATH, *files[RESOURCES_PATH]),
        put(ACTIONS_PATH, *files[ACTIONS_PATH]),
        put(KEYS_PATH, *files[KEYS_PATH]),
        f'printf %s {_b64(sudoers_content)} | base64 -d > "$tmp"\n',
        'visudo -cf "$tmp" >/dev/null || die "sudoers inválido"\n',
        f'install -o root -g root -m 0440 "$tmp" {SUDOERS_PATH}\n',
        'echo "enroll: ok"\n',
    ]
    return "".join(parts)


# --- SSH ------------------------------------------------------------------


def _ssh_target(app: App, config: Path) -> list[str]:
    if not HOST_RE.fullmatch(app.host) or not USER_RE.fullmatch(app.admin):
        raise EnrollError("host o admin de la ficha no válidos")
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts(config)}",
        "-o", "ConnectTimeout=10",
        f"{app.admin}@{app.host}",
    ]  # fmt: skip


def _as_admin(app: App, command: str) -> str:
    return command if app.admin == "root" else f"sudo -n {command}"


def _remote_current(app: App, config: Path, runner: Runner, path: str) -> str | None:
    probe = f"if [ -e {path} ]; then cat {path}; else exit 3; fi"
    proc = runner.run([*_ssh_target(app, config), _as_admin(app, f"sh -c {shlex.quote(probe)}")])
    if proc.returncode == 3:
        return None
    if proc.returncode != 0:
        raise EnrollError(f"no se pudo leer {path}: {proc.stderr.strip()}")
    return proc.stdout


def diff_remote(app: App, config: Path, files: dict[str, tuple[str, str]], runner: Runner) -> str:
    chunks = []
    for path, (content, _mode) in files.items():
        current = _remote_current(app, config, runner, path)
        old = (current or "").splitlines(keepends=True)
        label = path if current is not None else f"{path} (nuevo)"
        chunks += difflib.unified_diff(
            old, content.splitlines(keepends=True), f"a{label}", f"b{path}"
        )
    return "".join(chunks)


def verify(app: App, config: Path, key_ro: Path, run: Callable[..., Result] = run_verb) -> None:
    hosts = known_hosts(config)
    if not run(app, "status", key_ro, hosts).ok:
        raise EnrollError("verificación: `status` con la clave ro falló")
    rw_verbs = app.verbs("rw")
    if rw_verbs:
        attempt = run(app, rw_verbs[0], key_ro, hosts)
        if attempt.ok or attempt.code != 64:
            raise EnrollError(f"verificación: `{rw_verbs[0]}` con la clave ro NO fue rechazado")


def enroll(
    app: App,
    config: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    verify_key_ro: Path | None = None,
    runner: Runner | None = None,
    confirm: Callable[[str], bool] = lambda q: input(f"{q} [s/N] ").strip().lower() in ("s", "y"),
    say: Callable[[str], None] = print,
    run: Callable[..., Result] = run_verb,
) -> None:
    runner = runner or Runner()
    files = desired_files(app, config)
    diff = diff_remote(app, config, files, runner)
    say(diff or "(sin cambios)")
    if dry_run:
        return
    if not yes and not confirm(f"¿Aplicar en {app.host}?"):
        raise EnrollError("cancelado")
    proc = runner.run(
        [*_ssh_target(app, config), _as_admin(app, "bash -s")],
        input=remote_script(app, files),
    )
    say(proc.stdout.strip())
    if proc.returncode != 0:
        raise EnrollError(f"el script remoto falló: {proc.stderr.strip()}")
    if verify_key_ro:
        verify(app, config, verify_key_ro, run)
        say("verificación: ok")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="enroll", description=__doc__)
    p.add_argument("app")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--verify-key-ro", type=Path)
    args = p.parse_args(argv)
    try:
        app = load_app(args.config / "apps" / f"{args.app}.yaml")
        enroll(
            app,
            args.config,
            dry_run=args.dry_run,
            yes=args.yes,
            verify_key_ro=args.verify_key_ro,
        )
    except (EnrollError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
