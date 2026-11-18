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
SHELL_PATH = "/usr/lib/rescue/rescue-shell"
RESOURCES_PATH = "/usr/lib/rescue/resources.sh"
ACTIONS_PATH = "/etc/rescue/actions.conf"
SUDOERS_PATH = "/etc/sudoers.d/rescue"
KEYS_PATH = "/home/rescue/.ssh/authorized_keys"

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
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


RW_PROBE_VERB = "probe-rw"  # reservado en vm/rescue-shell

ROOT_ONLY_FN = r"""
root_only() {  # root_only <ruta>: la ruta y cada directorio hasta / son de root y sin g/o-w
  local p=$1
  [ -e "$p" ] || die "$p no existe"
  while :; do
    [ "$(stat -L -c %u "$p")" = 0 ] || die "$p no es de root"
    [ $(( 8#$(stat -L -c %a "$p") & 8#022 )) = 0 ] || die "$p escribible por grupo u otros"
    [ "$p" = / ] && break
    p=$(dirname "$p")
  done
}
root_only_real() {  # idem sobre la ruta original y sobre su destino si es un enlace
  root_only "$1"
  root_only "$(readlink -f "$1")"
}
# owner_only <ruta> <dir>: dentro de <dir>, de root o de $trusted_uids, sin o-w y sin g-w salvo
# grupo $trusted_gid; el primer ancestro fuera de <dir>, root_only.
owner_only() {
  local p u m
  for p in "$1" "$(readlink -f "$1")"; do
    [ -e "$p" ] || die "$p no existe"
    while [ "$p" = "$2" ] || [ "${p#"$2"/}" != "$p" ]; do
      u=$(stat -L -c %u "$p")
      case " 0 $trusted_uids " in
        *" $u "*) ;;
        *) die "$p no es de root ni de un dueño de confianza" ;;
      esac
      m=022
      [ -n "$trusted_gid" ] && [ "$(stat -L -c %g "$p")" = "$trusted_gid" ] && m=002
      [ $(( 8#$(stat -L -c %a "$p") & 8#$m )) = 0 ] || die "$p escribible por grupo u otros"
      p=$(dirname "$p")
    done
    root_only "$p"
  done
}
root_only_path() {  # ruta que puede no existir aún: su primer ancestro existente debe ser de root
  p="$1"
  while [ ! -e "$p" ] && [ ! -L "$p" ]; do p=$(dirname "$p"); done
  root_only_real "$p"
}
"""


def sudo_executables(actions: Sequence[Action]) -> list[str]:
    return sorted({a.argv[0] for a in actions if a.sudo})


def sudo_paths(actions: Sequence[Action]) -> list[str]:
    """argv[0] y todo argumento absoluto de las acciones sudo: root los ejecuta o los lee.

    Una ruta que no se pueda comprobar (relativa, embebida en una opción, con `.` o `..`)
    se rechaza: con sudo, toda ruta ha de ser absoluta y canónica.
    """
    for act in actions:
        if not act.sudo:
            continue
        for arg in act.argv:
            if "/" in arg and not arg.startswith("/") or {".", ".."} & set(arg.split("/")):
                raise EnrollError(f"{act.verb}: ruta no comprobable en acción sudo: {arg!r}")
    return sorted(
        {
            arg
            for act in actions
            if act.sudo
            for arg in act.argv
            if arg == act.argv[0] or arg.startswith("/")
        }
    )


def _sudo_guard(actions: Sequence[Action], skip: frozenset[str] = frozenset()) -> str:
    """`skip`: rutas exactas que ya comprueba el guard del proyecto compose de confianza."""
    exes = set(sudo_executables(actions))
    lines = []
    for path in sudo_paths(actions):
        if path in skip and path not in exes:
            continue
        q = shlex.quote(path)
        # argv[0] debe existir; un argumento absoluto que aún no existe no puede crearlo la app.
        lines.append(f"root_only_real {q}\n" if path in exes else f"root_only_path {q}\n")
    return "".join(lines)


def _valid_name(value: object) -> bool:
    return isinstance(value, str) and bool(USER_RE.fullmatch(value)) and value != "rescue"


def _trusted(preset: dict) -> tuple[tuple[str, ...], str | None]:
    """Dueños (`trusted_owner`: usuario o lista) y grupo (`trusted_group`) de confianza."""
    owner, group = preset.get("trusted_owner"), preset.get("trusted_group")
    if owner is None:
        if group is not None:
            raise EnrollError("`trusted_group` exige `trusted_owner`")
        return (), None
    owners = (owner,) if isinstance(owner, str) else owner
    if not isinstance(owners, (list, tuple)) or not owners or not all(map(_valid_name, owners)):
        raise EnrollError("`trusted_owner` no válido (nunca `rescue`)")
    if group is not None and not _valid_name(group):
        raise EnrollError("`trusted_group` no válido (nunca `rescue`)")
    return tuple(owners), group


def _compose_paths(preset: dict) -> list[str]:
    directory = preset["dir"]
    names = (*COMPOSE_FILES, preset["file"]) if "file" in preset else COMPOSE_FILES
    return [directory, *(f"{directory}/{name}" for name in names)]


def _trusted_prologue(owners: tuple[str, ...], group: str | None) -> str:
    lines = ['trusted_uids=""', 'trusted_gid=""']
    for o in owners:
        lines.append(f'uid=$(id -u {o}) || die "trusted_owner {o} no existe"')
        lines.append('trusted_uids="$trusted_uids $uid"')
    if group:
        names = " ".join(owners)
        lines += [
            f'trusted_gid=$(getent group {group} | cut -d: -f3)',
            f'[ -n "$trusted_gid" ] || die "trusted_group {group} no existe"',
            f'for m in $(getent group {group} | cut -d: -f4 | tr , " ") '
            f"$(getent passwd | awk -F: -v g=\"$trusted_gid\" '$4 == g {{print $1}}'); do",
            f'  case " {names} " in *" $m "*) ;; '
            f'*) die "$m está en trusted_group {group} y no es trusted_owner" ;; esac',
            "done",
        ]
    return "\n".join(lines) + "\n"


def _compose_guard(preset: dict) -> str:
    """El proyecto compose lo lee root vía sudo: sus ficheros no pueden ser de cualquiera.

    `trusted_owner` (opcional en la ficha) admite además a esos usuarios como dueños del
    directorio y sus ficheros: solo tiene sentido si ya equivalen a root (sudo ALL, grupo
    docker). `trusted_group` admite escritura de ese grupo si todos sus miembros son dueños
    de confianza; la VM lo comprueba al enrolar.
    """
    directory = shlex.quote(preset["dir"])
    paths = " ".join(shlex.quote(p) for p in _compose_paths(preset))
    owners, group = _trusted(preset)
    if not owners:
        check, prologue = 'root_only_real "$p"', ""
    else:
        check = f'owner_only "$p" {directory}'
        prologue = _trusted_prologue(owners, group)
    return f"""{prologue}
for p in {paths}; do
  [ -e "$p" ] || [ -L "$p" ] || [ "$p" = {directory} ] || continue  # un enlace colgante aborta
  {check}
done
"""


def remote_script(app: App, files: dict[str, tuple[str, str]]) -> str:
    preset = app.raw.get("preset") or {}
    compose = preset.get("kind") == "docker-compose"
    guard = _compose_guard(preset) if compose else ""
    trusted = compose and bool(_trusted(preset)[0])
    skip = frozenset(_compose_paths(preset)) if trusted else frozenset()

    def put(path: str, content: str, mode: str) -> str:
        return (
            f'printf %s {_b64(content)} | base64 -d > "$tmp"\n'
            f'install -o root -g root -m {mode} "$tmp" {path}\n'
        )

    sudoers_content, _ = files[SUDOERS_PATH]
    parts = [
        "set -euo pipefail\numask 022\n",
        'die() { echo "enroll: $*" >&2; exit 1; }\n',
        ROOT_ONLY_FN,
        "tmp=$(mktemp); trap 'rm -f \"$tmp\"' EXIT\n",
        _sudo_guard(app.actions, skip),
        guard,
        "id rescue >/dev/null 2>&1 || useradd --system --home-dir /home/rescue "
        "--no-create-home --shell /bin/bash rescue\n",
        # `*` no es contraseña válida pero tampoco bloquea la cuenta (pubkey sigue funcionando).
        "usermod -p '*' rescue\n",
        "install -d -o root -g root -m 0755 /home/rescue /home/rescue/.ssh "
        "/usr/lib/rescue /etc/rescue\n",
        put(SHELL_PATH, *files[SHELL_PATH]),
        put(RESOURCES_PATH, *files[RESOURCES_PATH]),
        f"root_only_real {SHELL_PATH}\nroot_only_real {RESOURCES_PATH}\n"
        "root_only_real /usr/lib/rescue\n",
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
    """Sin cambios remotos: un verbo ro funciona y la clave ro es rechazada (64) en el sondeo rw.

    La clave rw no se usa nunca y ningún verbo rw del catálogo se lanza: el sondeo es un verbo
    reservado de rescue-shell que no ejecuta nada.
    """
    hosts = known_hosts(config)
    ro_verbs = app.verbs("ro")
    if not ro_verbs:
        raise EnrollError("verificación: la app no tiene verbos ro")
    if not run(app, ro_verbs[0], key_ro, hosts).ok:
        raise EnrollError(f"verificación: `{ro_verbs[0]}` con la clave ro falló")
    attempt = run(app, RW_PROBE_VERB, key_ro, hosts)
    if attempt.ok or attempt.code != 64:
        raise EnrollError("verificación: la clave ro NO fue rechazada en el sondeo rw")


def enroll(
    app: App,
    config: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    runner: Runner | None = None,
    confirm: Callable[[str], bool] = lambda q: input(f"{q} [s/N] ").strip().lower() in ("s", "y"),
    say: Callable[[str], None] = print,
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


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="enroll", description=__doc__)
    p.add_argument("app")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--verify-only", action="store_true", help="solo verifica; sin cambios remotos")
    p.add_argument("--verify-key-ro", type=Path)
    args = p.parse_args(argv)
    try:
        app = load_app(args.config / "apps" / f"{args.app}.yaml")
        if args.verify_only:
            if not args.verify_key_ro:
                raise EnrollError("--verify-only exige --verify-key-ro")
            verify(app, args.config, args.verify_key_ro)
            print("verificación: ok")
        else:
            enroll(app, args.config, dry_run=args.dry_run, yes=args.yes)
    except (EnrollError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
