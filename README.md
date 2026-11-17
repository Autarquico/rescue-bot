# rescue-bot

Rescate de apps desde Slack. Cuando una app cae, una persona conversa con un agente en `#rescue`, el agente diagnostica con acceso **solo de lectura** y, si propone una acción (`restart`, `rollback`), solo se ejecuta tras un clic de una persona autorizada.

Spec: [`docs/specs/rescue-bot-v1.md`](docs/specs/rescue-bot-v1.md).

## Arquitectura

```
Healthchecks.io / Uptime Kuma
   │ (integración nativa de Slack: publican en #rescue)
   ▼
#rescue (Slack, privado)
   │ 1. aparece el aviso de caída
   │ 2. una persona menciona al bot: "@rescue ¿qué le pasa a delta?"
   ▼
Hermes Agent (CT rescue, usuario `agent`)
   │ 3. diagnostica con la clave de SOLO LECTURA ──SSH/Tailscale──► rescue-shell en la VM
   │ 4. propone una acción: herramienta request_action(app, verb)
   ▼
approver (CT rescue, usuario `approver`; otra app de Slack)
   │ 5. publica en #rescue: "¿Ejecutar restart en delta?" [Aprobar] [Rechazar]
   │ 6. una persona autorizada pulsa Aprobar
   │ 7. ejecuta con la clave de ESCRITURA ──SSH/Tailscale──► rescue-shell en la VM
   ▼
#rescue: resultado y comprobación de salud posterior
```

- Las alertas no pasan por el bot y no hay ningún endpoint expuesto a Internet (Slack en Socket Mode).
- El código es público; la configuración real (apps, IPs, IDs de Slack, claves públicas) vive en un repo privado. Ver `examples/config/`.

## Modelo de amenazas

| Capa | Qué protege | Qué no protege |
|---|---|---|
| `rescue-shell` + `actions.conf` en cada VM (propiedad de root, escrito solo por `enroll`) | El catálogo de verbos lo decide la VM: un bot comprometido no puede hacer más de lo que la VM permite. Sin argumentos libres, sin `sh -c` con texto externo, clave `ro` limitada a verbos `ro`. | Un verbo legítimo mal diseñado (p. ej. un `rollback` que haga daño). |
| Claves SSH con `restrict,from=<IP Tailscale del CT>,command=` | Las claves robadas no sirven desde otro nodo ni para abrir una shell. | Un atacante con root dentro del CT. |
| Usuarios `agent` / `approver` separados, secretos 0600 en directorios 0700 | El agente (el modelo) nunca ve la clave `rw` ni los tokens de aprobación. | Un fallo del kernel/LXC que rompa el aislamiento. |
| Socket Unix con `{app, verb, motivo}` y validación en `approver` | El agente solo puede *proponer* verbos `rw` existentes. | — |
| Aprobación humana: solo IDs de `approvers`, un solo uso, caduca a 10 min, límites 1/5 min y 3/h | Prompt injection no basta para ejecutar nada; no hay bucles de reinicios. | Un aprobador que pulse sin leer. |
| Plugin y config de Hermes de root y de solo lectura para `agent` | El agente no puede reescribir sus propias herramientas. | — |
| Tailnet: `tag:rescue → tag:app:22` únicamente | Sin acceso a Proxmox ni al resto de la LAN por Tailscale. | — |
| Firewall de Proxmox en el CT (`deploy/create-ct.sh`): entrada DROP, salida DROP a RFC1918 salvo DNS al gateway | El CT no alcanza la LAN ni Proxmox; Tailscale (encapsulado por Internet) sigue funcionando. | Solo se aplica con el firewall del datacenter activado (ver abajo). |
| `gitleaks` en CI | El repo público no filtra IPs reales, hostnames `*.ts.net`, ni secretos. | Secretos ya en el historial de otro repo. |

Datos hacia el modelo: los logs de diagnóstico van al proveedor del modelo con redacción básica (tokens, claves privadas, `password=`); es incompleta por diseño. Todo lo que devuelve una VM es dato no fiable; la garantía real son las capas anteriores, no el prompt.

## Quickstart de despliegue

1. **Crear el CT** (en devsrv02, como root): `deploy/create-ct.sh [CTID]` (por defecto 120) `[LAN_GATEWAY]` (por defecto 192.168.0.1). Activa el firewall de Proxmox en el CT (`/etc/pve/firewall/<CTID>.fw`: salida bloqueada a RFC1918, `fe80::/10`, `fc00::/7` y las direcciones IPv6 del nodo) y crea `net0` sin `ip6`; `provision.sh` desactiva IPv6 en el CT (`/etc/sysctl.d/90-rescue.conf`). Si el firewall del datacenter no está activado (`enable: 1` en `/etc/pve/firewall/cluster.fw`) o `net0` no tiene `firewall=1`, el script termina con error. `--enable-datacenter-firewall` activa el del datacenter fijando `policy_in` y `policy_out` a ACCEPT explícitos (sin definir, Proxmox aplicaría DROP a los nodos y cortaría Tailscale, PBS, etc.); si ya tienen otro valor, imprime `[OPTIONS]` y se niega. Sin SSH entrante salvo `MGMT_CIDR=<cidr>`; se entra por Tailscale o `pct exec`.
2. **Provisionar** (dentro del CT, como root):
   ```
   /root/.ssh/config_deploy_key   # deploy key de SOLO LECTURA del repo privado de configuración (0600)
   deploy/provision.sh --repo <url del repo público> --config-repo <url del repo privado> [--ref main]
   ```
   Autentica Tailscale con el enlace que imprime (`--advertise-tags=tag:rescue --ssh=false`). Al terminar imprime las dos claves públicas.
3. **Claves al repo de configuración**: guarda `keys/key_ro.pub` y `keys/key_rw.pub`, haz commit y push. Anota la IP Tailscale del CT en `settings.yaml` (`rescue_host.tailscale_ip`).
4. **Apps de Slack**: crea dos apps desde `deploy/slack-manifests/rescue.yaml` y `rescue-approvals.yaml`; genera el bot token (`xoxb-`) y el app-level token (`xapp-`, scope `connections:write`) de cada una; invita ambas a `#rescue`.
5. **Secretos**: desde tu portátil, `ssh -t root@<ct> /opt/rescue-bot/deploy/install-secrets.sh`. Los valores se piden sin eco y nunca se escriben en git.
6. **ACL del tailnet**: fusiona `deploy/tailnet-acl.hujson` en tu política (instrucciones en el fichero) y etiqueta las VMs con `tag:app`.
7. **Verificar**: `/opt/rescue-bot/deploy/verify-hermes.sh` en el CT.
8. **Añadir apps**: `/add-to-sre` (ver abajo).

### Instalación de Hermes (no verificada)

`provision.sh` instala Hermes con `uv tool install "$HERMES_SPEC"` como root en `/opt/hermes` con el binario en `/usr/local/bin/hermes`, de modo que el agente no pueda modificar su propio ejecutable. `HERMES_SPEC` está fijado por defecto a `hermes-agent @ git+https://github.com/NousResearch/hermes-agent@v2026.6.5` (etiqueta citada en la documentación upstream; que el repo se instale así con `uv` no está verificado). Si el paquete o el instalador oficial difieren, exporta `HERMES_SPEC` o adapta esa sección. `/var/lib/rescue-agent` es de root y `.hermes` es `root:agent` con modo 1775: Hermes escribe su estado ahí (es lo único escribible en la unidad, `ReadWritePaths=`, sin `StateDirectory=`), y el bit sticky impide al agente borrar o renombrar `config.yaml`, `SOUL.md` y `plugins/`, que son de root. Igualmente, `verify-hermes.sh` asume `hermes tools list --platform slack` (configurable con `HERMES_TOOLS_CMD`).

## Añadir una app

Con Claude Code: `/add-to-sre` (skill en `skills/add-to-sre/`), que inspecciona la VM en modo lectura, propone el preset y sigue este flujo: `bin/add-app <app> --config <dir-config> ... --dry-run` (sin efectos: imprime la ficha YAML, las huellas de las host keys y el diff de enroll) → se muestra → confirmación del usuario → la misma orden sin `--dry-run`, que da de alta la VM, sube la config, recarga el CT con `/usr/local/sbin/rescue-reload` y verifica con `/usr/local/sbin/rescue-verify <app>`.

Ambos scripts se instalan en el CT (root, 0755) desde `deploy/rescue-reload` y `deploy/rescue-verify`. `rescue-reload` hace `git pull --ff-only` de la config con la deploy key, reaplica propietario/modos y reinicia `rescue-approver` y `hermes-gateway`. `rescue-verify` valida el nombre de la app y ejecuta `rescue-enroll --verify-only` como `agent` con la clave `ro`.

En las VMs, el usuario `rescue` tiene shell `/bin/bash`: el `command=` forzado la necesita real; `restrict` + `command=` en `authorized_keys` lo confinan. En la ficha de la app, `vm_actions` son listas argv; `actions:` se deriva, no se declara.

## Contrato de rollback

Lo implementa cada app; el bot solo lo invoca.

- Tras un despliegue con la salud en verde, el CI/despliegue guarda la versión (etiqueta, imagen o commit) en `/var/lib/<app>/last-good`, fuera del ejecutable.
- `rollback_cmd` apunta a un script de root (p. ej. `/usr/local/lib/<app>/rollback-to-last-good`, 0755) que vuelve a esa versión y devuelve 0 solo si la app pasa después su comprobación de salud. Ese script lee `last-good` como dato y valida su formato antes de usarlo.
- enroll aborta si el ejecutable, cualquier argumento con ruta absoluta o alguno de sus directorios padre no es de root o es escribible por grupo u otros: lo que corre con sudo no puede estar en manos de la app.
- Una app sin este contrato no declara `rollback`.

## Desarrollo

```
uv sync
uv run ruff check .
uv run pytest
shellcheck vm/rescue-shell vm/*.sh deploy/*.sh
```

CI (`.github/workflows/ci.yml`) ejecuta lo mismo más `gitleaks` con `.gitleaks.toml` (bloquea IPs Tailscale e `*.ts.net` reales; permite `100.64.0.10` y `example.ts.net`).
