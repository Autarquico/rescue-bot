#!/usr/bin/env bash
# Crea el CT `rescue` en el nodo Proxmox devsrv02. Ejecutar como root en el nodo.
# Uso: create-ct.sh [CTID]        (por defecto 120)
# Variables opcionales: STORAGE (local-lvm), TEMPLATE_STORAGE (local), BRIDGE (vmbr0), HOSTNAME_CT (rescue)
set -euo pipefail

CTID="${1:-120}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
HOSTNAME_CT="${HOSTNAME_CT:-rescue}"

[[ $EUID -eq 0 ]] || { echo "ejecuta como root en devsrv02" >&2; exit 1; }
command -v pct >/dev/null || { echo "pct no encontrado: ¿estás en el nodo Proxmox?" >&2; exit 1; }

if pct status "$CTID" >/dev/null 2>&1; then
  echo "el CT $CTID ya existe; no se hace nada"
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
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --ssh-public-keys /root/.ssh/authorized_keys

# /dev/net/tun para Tailscale
conf="/etc/pve/lxc/${CTID}.conf"
cat >>"$conf" <<'TUN'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
TUN

pct start "$CTID"
echo "CT $CTID creado y arrancado."
echo "Siguiente: pct exec $CTID -- bash  y ejecutar deploy/provision.sh (ver README)."
