#!/usr/bin/env bash
# Crea el CT `rescue` en el nodo Proxmox devsrv02. Ejecutar como root en el nodo.
# Uso: create-ct.sh [CTID] [LAN_GATEWAY]   (por defecto 120 y 192.168.0.1)
# Variables opcionales: STORAGE (local-lvm), TEMPLATE_STORAGE (local), BRIDGE (vmbr0), HOSTNAME_CT (rescue),
#   MGMT_CIDR (origen permitido para SSH entrante; vacío = ninguno, se entra por Tailscale o `pct exec`)
#
# Cortafuegos: el CT no debe alcanzar la LAN ni Proxmox, ni por IPv4 ni por IPv6. Se activa firewall=1 en net0
# (sin ip6: la IPv6 se desactiva dentro del CT en provision.sh) y se escribe /etc/pve/firewall/<CTID>.fw
# (entrada DROP; salida ACCEPT salvo RFC1918, fe80::/10, fc00::/7 y las direcciones IPv6 del nodo; DNS al gateway permitido).
# Tailscale va encapsulado por Internet, así que sigue funcionando.
# REQUISITO: cortafuegos del datacenter activado (`enable: 1` en [OPTIONS] de /etc/pve/firewall/cluster.fw).
# Si no lo está el script TERMINA con error (exit 1). Con --enable-datacenter-firewall lo activa, pero solo si
# policy_in/policy_out del datacenter son ACCEPT o no están definidas; las fija a ACCEPT explícito, porque sin
# definir Proxmox aplica DROP a los nodos (si no, imprime [OPTIONS] y se niega).
# Si net0 no tiene firewall=1 tras configurar, también termina con error.
# Uso: create-ct.sh [--enable-datacenter-firewall] [CTID] [LAN_GATEWAY]
set -euo pipefail

ENABLE_DC_FW=0
args=()
for a in "$@"; do
  case "$a" in
    --enable-datacenter-firewall) ENABLE_DC_FW=1 ;;
    *) args+=("$a") ;;
  esac
done
set -- "${args[@]+"${args[@]}"}"

CTID="${1:-120}"
LAN_GATEWAY="${2:-192.168.0.1}"
MGMT_CIDR="${MGMT_CIDR:-}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
HOSTNAME_CT="${HOSTNAME_CT:-rescue}"
CLUSTER_FW=/etc/pve/firewall/cluster.fw

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "ejecuta como root en devsrv02"
command -v pct >/dev/null || die "pct no encontrado: ¿estás en el nodo Proxmox?"

cluster_options() { awk '/^\[/{s=$0; next} s=="[OPTIONS]" && NF' "$CLUSTER_FW" 2>/dev/null || true; }

datacenter_firewall_enabled() { cluster_options | grep -q '^enable: *1'; }

enable_datacenter_firewall() {
  # Con enable: 1 y policy_in sin definir, Proxmox aplica DROP a los nodos (solo LAN→8006/22 pasa):
  # se cortarían Tailscale, PBS y cualquier otro servicio del nodo. Por eso se fijan ACCEPT explícitos,
  # que dejan nodos e invitados como estaban; solo el CT (firewall=1 en net0) recibe reglas.
  local key value
  for key in policy_in policy_out; do
    value="$(cluster_options | sed -n "s/^$key: *//p" | tr -d '[:space:]')"
    if [[ -n "$value" && "$value" != ACCEPT ]]; then
      echo "[OPTIONS] actuales de $CLUSTER_FW:" >&2
      cluster_options >&2
      die "$key del datacenter es '$value': actívalo a mano tras revisar sus reglas."
    fi
  done
  pvesh set /cluster/firewall/options --policy_in ACCEPT --policy_out ACCEPT --enable 1
  echo "cortafuegos del datacenter activado con policy_in/policy_out ACCEPT explícitos"
}

ensure_datacenter_firewall() {
  datacenter_firewall_enabled && return 0
  if ((ENABLE_DC_FW)); then
    enable_datacenter_firewall
  else
    die "el cortafuegos del datacenter no está activado ($CLUSTER_FW [OPTIONS] enable: 1); las reglas del CT $CTID no se aplicarían. Actívalo o reejecuta con --enable-datacenter-firewall."
  fi
  datacenter_firewall_enabled || die "no se pudo activar el cortafuegos del datacenter"
}

verify_net0() {
  local net0
  net0="$(pct config "$CTID" | grep '^net0:' || true)"
  [[ "$net0" == *firewall=1* ]] || die "net0 del CT $CTID no tiene firewall=1; ejecuta: pct set $CTID --net0 <valor actual>,firewall=1"
  [[ "$net0" != *ip6=* ]] || die "net0 del CT $CTID tiene ip6=...; quítalo (IPv6 debe ir desactivada): pct set $CTID --net0 <valor actual sin ip6>"
}

host_ipv6_addrs() { ip -6 -o addr show 2>/dev/null | awk '{split($4,a,"/"); print a[1]}' | grep -vi '^fe80:' | sort -u || true; }

write_firewall() {
  local fw="/etc/pve/firewall/${CTID}.fw"
  mkdir -p /etc/pve/firewall
  {
    printf '[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: ACCEPT\ndhcp: 1\n\n[RULES]\n'
    [[ -z "$MGMT_CIDR" ]] || printf 'IN ACCEPT -source %s -p tcp -dport 22\n' "$MGMT_CIDR"
    printf 'OUT ACCEPT -dest %s -p udp -dport 53\n' "$LAN_GATEWAY"
    printf 'OUT ACCEPT -dest %s -p tcp -dport 53\n' "$LAN_GATEWAY"
    printf 'OUT DROP -dest %s\n' 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
    printf 'OUT DROP -dest %s\n' fe80::/10 fc00::/7
    while read -r addr; do
      [[ -z "$addr" ]] || printf 'OUT DROP -dest %s/128\n' "$addr"
    done < <(host_ipv6_addrs)
  } >"${fw}.new"
  mv "${fw}.new" "$fw"
}

ensure_datacenter_firewall

if pct status "$CTID" >/dev/null 2>&1; then
  write_firewall
  verify_net0
  echo "el CT $CTID ya existe; solo se reaplica el cortafuegos"
  exit 0
fi

arch="$(dpkg --print-architecture)"
pattern="debian-13-standard_.*_${arch}\\.tar"
template="$(pveam list "$TEMPLATE_STORAGE" | awk -v p="$pattern" '$1 ~ p {print $1}' | sort -V | tail -n1)"
if [[ -z "$template" ]]; then
  pveam update
  name="$(pveam available --section system | awk -v p="$pattern" '$2 ~ p {print $2}' | sort -V | tail -n1)"
  [[ -n "$name" ]] || die "no hay plantilla debian-13-standard para $arch"
  pveam download "$TEMPLATE_STORAGE" "$name"
  template="${TEMPLATE_STORAGE}:vztmpl/${name}"
fi

pct create "$CTID" "$template" --arch "$arch" \
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
verify_net0

pct start "$CTID"
echo "CT $CTID creado y arrancado."
echo "Siguiente: pct exec $CTID -- bash  y ejecutar deploy/provision.sh (ver README)."
