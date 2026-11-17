#!/usr/bin/env bash
# Verifica el aislamiento de Hermes. Ejecutar como root en el CT.
# NO VERIFICADO: la sintaxis exacta de `hermes tools list --platform slack`.
# Si el subcomando difiere, ajusta HERMES_TOOLS_CMD.
set -uo pipefail
AGENT_HOME=/var/lib/rescue-agent
HERMES_TOOLS_CMD="${HERMES_TOOLS_CMD:-hermes tools list --platform slack}"
fail=0
bad() { echo "FALLO: $*" >&2; fail=1; }
ok() { echo "ok: $*"; }

# 1. único toolset habilitado: rescue
read -ra tools_cmd <<<"$HERMES_TOOLS_CMD"
out="$(runuser -u agent -- env HOME="$AGENT_HOME" HERMES_HOME="$AGENT_HOME/.hermes" \
  RESCUE_CONFIG=/etc/rescue-bot/config "${tools_cmd[@]}" 2>&1)" || bad "no se pudo ejecutar: $HERMES_TOOLS_CMD"
echo "$out"
enabled="$(grep -iE 'enabled|✓|\[x\]' <<<"$out" | grep -viE 'disabled' || true)"
if [[ -z "$enabled" ]]; then
  bad "no se detectaron toolsets habilitados (revisar formato de salida)"
elif grep -viE 'rescue' <<<"$enabled" | grep -q .; then
  bad "hay toolsets habilitados distintos de rescue:"; grep -viE 'rescue' <<<"$enabled" >&2
else
  ok "solo rescue habilitado"
fi

# 2. plugin y config: root y no escribibles por agent
for p in "$AGENT_HOME" "$AGENT_HOME/.hermes" "$AGENT_HOME/.hermes/config.yaml" "$AGENT_HOME/.hermes/SOUL.md" "$AGENT_HOME/.hermes/plugins"; do
  [[ "$(stat -c %U "$p")" == root ]] || bad "$p no es de root"
done
while IFS= read -r -d '' f; do
  [[ "$(stat -c %U "$f")" == root ]] || bad "$f no es de root"
  runuser -u agent -- test -w "$f" && bad "agent puede escribir en $f"
done < <(find "$AGENT_HOME/.hermes/plugins" "$AGENT_HOME/.hermes/config.yaml" "$AGENT_HOME/.hermes/SOUL.md" -print0)
runuser -u agent -- test -w "$AGENT_HOME" && bad "agent puede escribir en $AGENT_HOME"
for d in sessions logs memories cache workspace; do
  [[ "$(stat -c %U "$AGENT_HOME/.hermes/$d")" == agent ]] || bad "$AGENT_HOME/.hermes/$d no es de agent (estado)"
done
runuser -u agent -- test -w "$AGENT_HOME/.hermes" && bad "agent puede escribir en .hermes (podría sustituir plugin/config)"
runuser -u agent -- test -w /etc/rescue-bot/config && bad "agent puede escribir en la configuración"
[[ $fail -eq 0 ]] && ok "plugin y config de root, no escribibles por agent"

# 3. agent no lee los secretos de approver
runuser -u agent -- ls /etc/rescue-bot/approver >/dev/null 2>&1 && bad "agent lista /etc/rescue-bot/approver"
for f in key_rw slack.env; do
  runuser -u agent -- cat "/etc/rescue-bot/approver/$f" >/dev/null 2>&1 && bad "agent lee approver/$f"
done
# y approver no lee los de agent
for f in key_ro hermes.env; do
  runuser -u approver -- cat "/etc/rescue-bot/agent/$f" >/dev/null 2>&1 && bad "approver lee agent/$f"
done
[[ $fail -eq 0 ]] && ok "secretos separados"

exit $fail
