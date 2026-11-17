#!/usr/bin/env bash
# Crea el CT `rescue` en el nodo Proxmox devsrv02. Ejecutar como root en el nodo.
# Uso: create-ct.sh [CTID] [LAN_GATEWAY]   (por defecto 120 y 192.168.0.1)
# Variables opcionales: STORAGE (local-lvm), TEMPLATE_STORAGE (local), BRIDGE (vmbr0), HOSTNAME_CT (rescue),
#   MGMT_CIDR (origen permitido para SSH entrante; vacío = ninguno, se entra por Tailscale o `pct exec`)
#
# Cortafuegos: el CT no debe alcanzar la LAN ni Proxmox. Se activa firewall=1 en net0 y se escribe
# /etc/pve/firewall/<CTID>.fw (entrada DROP; salida ACCEPT salvo RFC1918, con DNS al gateway permitido).
# Tailscale va encapsulado por Internet, así que sigue funcionando.
# REQUISITO: el cortafuegos del datacenter debe estar activado para que se apliquen las reglas del CT
# (Datacenter > Firewall > Options > Firewall = Yes, es decir `enable: 1` en [OPTIONS] de
# /etc/pve/firewall/cluster.fw, y servicio pve-firewall activo). Este script NO lo cambia ni toca la
# política del datacenter (policy_in sigue como esté, ACCEPT por defecto), para no afectar a otros invitados.
set -euo pipefail

CTID="${1:-120}"
LAN_GATEWAY="${2:-192.168.0.1}"
MGMT_CIDR="${MGMT_CIDR:-}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
HOSTNAME_CT="${HOSTNAME_CT:-rescue}"

[[ $EUID -eq 0 ]] || { echo "ejecuta como root en devsrv02" >&2; exit 1; }
command -v pct >/dev/null || { echo "pct no encontrado: ¿estás en el nodo Proxmox?" >&2; exit 1; }

write_firewall() {
  local fw="/etc/pve/firewall/${CTID}.fw"
  mkdir -p /etc/pve/firewall
  {
    printf '[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: ACCEPT\ndhcp: 1\n\n[RULES]\n'
    [[ -z "$MGMT_CIDR" ]] || printf 'IN ACCEPT -source %s -p tcp -dport 22\n' "$MGMT_CIDR"
    printf 'OUT ACCEPT -dest %s -p udp -dport 53\n' "$LAN_GATEWAY"
    printf 'OUT ACCEPT -dest %s -p tcp -dport 53\n' "$LAN_GATEWAY"
    printf 'OUT DROP -dest %s\n' 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
  } >"${fw}.new"
  mv "${fw}.new" "$fw"
  if ! grep -qs '^enable: *1' /etc/pve/firewall/cluster.fw; then
    echo "AVISO: el cortafuegos del datacenter no está activado; las reglas del CT $CTID NO se aplican hasta activarlo" >&2
    echo "       (cluster.fw [OPTIONS] enable: 1). No se modifica desde este script." >&2
  fi
}

if pct status "$CTID" >/dev/null 2>&1; then
  write_firewall
  pct config "$CTID" | grep -q '^net0:.*firewall=1' \
    || echo "AVISO: net0 del CT $CTID no tiene firewall=1; ejecuta: pct set $CTID --net0 <valor actual>,firewall=1" >&2
  echo "el CT $CTID ya existe; solo se reaplica el cortafuegos"
  exit 0
fi

template="$(pveam list "$TEMPLATE_STORAGE" | awk '/debian-13-standard/ {print $1}' | sort | tail -n1)"
if [[ -z "$template" ]]; then
  pveam update
  name="$(pveam available --section system | awk '/debian-13-standard/ {print $2}' | sort | tail -n1)"
  [[ -n "$name" ]] || { echo "no hay plantilla debian-13-standard disponible" >&2; exit 1; }
  pveam download "$TEMPLATE_STORAGE" "$name"
  template="${TEMPLATE_STORAGE}:vztmpl/${name}"
fi

pct create "$CTID" "$template" \
  --hostname "$HOSTNAME_CT" \
  --unprivileged 1 \
  --cores 1 \
  --memory 1024 \
  --swap 0 \
  --rootfs "${STORAGE}:8" \
  --features nesting=1 \
  --onboot 1 \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
  --ssh-public-keys /root/.ssh/authorized_keys

# /dev/net/tun para Tailscale
conf="/etc/pve/lxc/${CTID}.conf"
cat >>"$conf" <<'TUN'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
TUN

write_firewall

pct start "$CTID"
echo "CT $CTID creado y arrancado."
echo "Siguiente: pct exec $CTID -- bash  y ejecutar deploy/provision.sh (ver README)."
