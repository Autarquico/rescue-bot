#!/usr/bin/env bash
# Pide los secretos y escribe hermes.env y slack.env. Ejecutar como root en el CT.
# Desde el portátil:
#   ssh -t root@<ct> /opt/rescue-bot/deploy/install-secrets.sh
# Nunca imprime los valores. Se piden con read -s.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "ejecuta como root" >&2; exit 1; }
[[ -t 0 ]] || { echo "necesita un terminal (usa ssh -t)" >&2; exit 1; }

ask() { # <variable> <etiqueta> [visible]
  local v
  if [[ "${3:-}" == visible ]]; then
    read -r -p "$2: " v
  else
    read -r -s -p "$2: " v
    echo >&2
  fi
  [[ -n "$v" ]] || { echo "valor vacío para $1" >&2; exit 1; }
  [[ "$v" != *[[:space:]\'\"]* ]] || { echo "valor no válido para $1 (contiene espacios o comillas)" >&2; exit 1; }
  printf -v "$1" '%s' "$v"
}

ask AGENT_BOT "Slack (app rescue) Bot token xoxb-"
ask AGENT_APP "Slack (app rescue) App-level token xapp-"
ask ALLOWED "Slack user IDs permitidos para hablar con el agente (coma-separados)" visible
ask DEEPSEEK "DEEPSEEK_API_KEY"
ask APPR_BOT "Slack (app rescue-approvals) Bot token xoxb-"
ask APPR_APP "Slack (app rescue-approvals) App-level token xapp-"

write_env() { # <fichero> <dueño> <líneas...>
  local file="$1" owner="$2"
  shift 2
  local tmp
  tmp="$(mktemp -p "$(dirname "$file")")"
  printf '%s\n' "$@" >"$tmp"
  chown "$owner:$owner" "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$file"
}

write_env /etc/rescue-bot/agent/hermes.env agent \
  "SLACK_BOT_TOKEN=$AGENT_BOT" "SLACK_APP_TOKEN=$AGENT_APP" \
  "SLACK_ALLOWED_USERS=$ALLOWED" "DEEPSEEK_API_KEY=$DEEPSEEK"
write_env /etc/rescue-bot/approver/slack.env approver \
  "SLACK_BOT_TOKEN=$APPR_BOT" "SLACK_APP_TOKEN=$APPR_APP"

echo "Escritos hermes.env (agent, 0600) y slack.env (approver, 0600)."
systemctl restart rescue-approver.service hermes-gateway.service
systemctl --no-pager --lines=0 status rescue-approver.service hermes-gateway.service || true
