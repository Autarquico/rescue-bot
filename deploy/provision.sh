#!/usr/bin/env bash
# Aprovisiona el CT `rescue`. Ejecutar como root dentro del CT. Idempotente.
# Uso: provision.sh --repo URL --config-repo URL [--ref main]
#
# Requisitos previos:
#   - /root/.ssh/config_deploy_key: deploy key de SOLO LECTURA del repo privado de
#     configuración (si el repo se sirve por SSH). Se usa únicamente aquí, como root.
#   - Variable opcional HERMES_SPEC: paquete de Hermes para `uv tool install`
#     (por defecto fijado a la etiqueta v2026.6.5 en git). Ver README: el empaquetado no está verificado.
set -euo pipefail

REPO="" CONFIG_REPO="" REF="main"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --config-repo) CONFIG_REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    *) echo "argumento desconocido: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$REPO" && -n "$CONFIG_REPO" ]] || { echo "uso: $0 --repo URL --config-repo URL [--ref main]" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "ejecuta como root" >&2; exit 1; }

CODE=/opt/rescue-bot
CONFIG=/etc/rescue-bot/config
AGENT_HOME=/var/lib/rescue-agent
HERMES_SPEC="${HERMES_SPEC:-hermes-agent @ git+https://github.com/NousResearch/hermes-agent@v2026.6.5}"
DEPLOY_KEY=/root/.ssh/config_deploy_key

echo "== paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates unattended-upgrades openssh-client
systemctl enable --now unattended-upgrades

echo "== tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  tailscale up --advertise-tags=tag:rescue --ssh=false
fi
echo "IP Tailscale del CT: $(tailscale ip -4 | head -n1)  (va en settings.yaml -> rescue_host.tailscale_ip)"

echo "== usuarios y grupos"
getent group rescue-ipc >/dev/null || groupadd --system rescue-ipc
id agent >/dev/null 2>&1 || useradd --system --home-dir "$AGENT_HOME" --shell /usr/sbin/nologin agent
id approver >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin approver
usermod -aG rescue-ipc agent
usermod -aG rescue-ipc approver

echo "== uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

echo "== código en $CODE"
if [[ -d "$CODE/.git" ]]; then
  git -C "$CODE" fetch --quiet origin
else
  git clone --quiet "$REPO" "$CODE"
  git -C "$CODE" fetch --quiet origin
fi
git -C "$CODE" checkout --quiet "$REF"
git -C "$CODE" merge --ff-only --quiet "origin/$REF" 2>/dev/null || true
(cd "$CODE" && UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --frozen --no-dev)
chown -R root:root "$CODE"
chmod -R go+rX,go-w "$CODE" /opt/uv-python 2>/dev/null || true

echo "== configuración privada en $CONFIG"
install -d -m 0755 /etc/rescue-bot
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
[[ -f "$DEPLOY_KEY" ]] && GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
if [[ -d "$CONFIG/.git" ]]; then
  git -C "$CONFIG" pull --ff-only --quiet
else
  git clone --quiet "$CONFIG_REPO" "$CONFIG" \
    || { echo "no se pudo clonar la configuración; coloca la deploy key de lectura en $DEPLOY_KEY (0600) y reintenta" >&2; exit 1; }
fi
unset GIT_SSH_COMMAND
chown -R root:root "$CONFIG"
chmod -R u=rwX,go=rX "$CONFIG"

echo "== secretos y claves SSH"
install -d -m 0700 -o agent -g agent /etc/rescue-bot/agent
install -d -m 0700 -o approver -g approver /etc/rescue-bot/approver
install -d -m 0750 -o approver -g approver /var/log/rescue
gen_key() { # <fichero> <dueño> <comentario>
  if [[ ! -f "$1" ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "$3" -f "$1"
  fi
  chown "$2:$2" "$1" "$1.pub"
  chmod 0600 "$1"
  chmod 0644 "$1.pub"
}
gen_key /etc/rescue-bot/agent/key_ro agent rescue-bot-ro
gen_key /etc/rescue-bot/approver/key_rw approver rescue-bot-rw
# el .pub no es secreto; se deja legible solo dentro del directorio 0700 del dueño

echo "== Hermes"
export UV_TOOL_DIR=/opt/hermes UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/uv-python
# rescue_bot va en el mismo entorno que Hermes: el plugin importa config, ssh y health.
uv tool install --force "$HERMES_SPEC" --with "$CODE"
chmod -R go+rX,go-w /opt/hermes /opt/uv-python 2>/dev/null || true

# $AGENT_HOME y .hermes son de root: el agente no puede renombrar ni sustituir plugin/config.
# Solo los subdirectorios de estado son suyos.
install -d -m 0755 -o root -g root "$AGENT_HOME" "$AGENT_HOME/.hermes" "$AGENT_HOME/.hermes/plugins"
for d in sessions logs memories cache workspace; do
  install -d -m 0700 -o agent -g agent "$AGENT_HOME/.hermes/$d"
done
rm -rf "$AGENT_HOME/.hermes/plugins/rescue"
cp -r "$CODE/engines/hermes/plugin/rescue" "$AGENT_HOME/.hermes/plugins/rescue"
chown -R root:root "$AGENT_HOME/.hermes/plugins"
chmod -R u=rwX,go=rX "$AGENT_HOME/.hermes/plugins"
install -m 0644 -o root -g root "$CODE/engines/hermes/SOUL.md" "$AGENT_HOME/.hermes/SOUL.md"
install -m 0644 -o root -g root "$CODE/engines/hermes/config.yaml.tmpl" "$AGENT_HOME/.hermes/config.yaml"

echo "== scripts de administración"
install -m 0755 -o root -g root "$CODE/deploy/rescue-reload" /usr/local/sbin/rescue-reload
install -m 0755 -o root -g root "$CODE/deploy/rescue-verify" /usr/local/sbin/rescue-verify

echo "== unidades systemd"
install -m 0644 "$CODE/deploy/rescue-approver.service" /etc/systemd/system/
install -m 0644 "$CODE/deploy/hermes-gateway.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable rescue-approver.service hermes-gateway.service

missing=()
[[ -f /etc/rescue-bot/agent/hermes.env ]] || missing+=(/etc/rescue-bot/agent/hermes.env)
[[ -f /etc/rescue-bot/approver/slack.env ]] || missing+=(/etc/rescue-bot/approver/slack.env)
if ((${#missing[@]})); then
  echo "Servicios habilitados pero NO arrancados. Faltan: ${missing[*]}"
  echo "Desde tu portátil: ssh -t root@<ct> /opt/rescue-bot/deploy/install-secrets.sh"
else
  systemctl restart rescue-approver.service hermes-gateway.service
fi

cat <<MSG

== Claves públicas (copiar al repo privado de configuración, carpeta keys/)
-- keys/key_ro.pub
$(cat /etc/rescue-bot/agent/key_ro.pub)
-- keys/key_rw.pub
$(cat /etc/rescue-bot/approver/key_rw.pub)

Luego: commit + push en el repo de configuración y, en el CT, /usr/local/sbin/rescue-reload.
Las VMs se dan de alta con bin/enroll / bin/add-app, que leen esas claves.
MSG
