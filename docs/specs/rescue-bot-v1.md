---
codename: rescue-bot
title: rescue-bot v1 — rescate de apps desde Slack
type: epic
status: active
priority: P1
size: L
issue: null
prs: []
related_issues: [1]
depends_on: []
supersedes: []
related_specs: []
created: 2026-11-17
updated: 2026-11-17
---

# rescue-bot v1 — rescate de apps desde Slack

## Contexto / problema

Cuando una app cae, avisar en Slack y permitir que una persona, conversando con un
agente en ese mismo canal, diagnostique la app y la levante de forma momentánea.
Ejemplos: reiniciar el servicio, o volver a la última versión que pasó su
comprobación de salud.

Requisitos que guían todo el diseño:

- **Seguro por construcción.** El agente puede equivocarse o ser manipulado (por
  ejemplo, con texto inyectado en un log). Aun así, nunca puede hacer en una VM más
  de lo que esa VM permite.
- **Mínimo.** Pocas piezas, sin bases de datos ni paneles.
- **Añadir una app es añadir un fichero** y ejecutar una vez el alta de la VM.

## Scope

**Dentro:**

- Alertas de caída en un canal de Slack privado.
- Diagnóstico de solo lectura bajo demanda (estado, logs, salud HTTP, recursos).
- Acciones que cambian cosas, siempre con aprobación humana: `restart` y `rollback`.
- Acceso a las VMs por SSH a través de Tailscale.
- Registro de propuestas, aprobaciones y ejecuciones.

**Fuera, en v1:**

- Arreglos automáticos sin aprobación.
- Acceso a la API de Proxmox, a Cloudflare o a bases de datos.
- Desplegar. El rollback lo implementa cada app; el bot solo lo invoca (ver «Contrato de rollback»).
- Integración con supermgmt. Queda como evolución posible: sustituiría el acceso
  SSH propio por MCP más los planes de supermgmt.
- Métricas, paneles y logs centralizados.

## Criterios de aceptación

### Scenario: la alerta llega a Slack sin pasar por el bot
- **Given** Healthchecks marca `delta` como caída
- **When** se dispara la alerta
- **Then** aparece un mensaje en `#rescue` sin que intervenga el bot

### Scenario: el diagnóstico solo usa verbos de lectura
- **Given** un mensaje `@rescue ¿qué le pasa a delta?`
- **When** el agente diagnostica
- **Then** solo usa verbos `ro` y responde con su hipótesis y las evidencias

### Scenario: la clave de lectura no puede escribir
- **Given** el agente con la clave de solo lectura
- **When** intenta `restart` directamente por SSH
- **Then** `rescue-shell` lo rechaza y queda registrado

### Scenario: un verbo inexistente se rechaza
- **Given** un verbo que no está en `actions.conf` (por ejemplo `rm`, `bash` o `status; id`)
- **When** llega por cualquiera de las dos claves
- **Then** se rechaza sin ejecutar nada

### Scenario: una aprobación válida ejecuta y verifica
- **Given** el agente propone `restart` en `delta`
- **When** un usuario de `approvers` pulsa Aprobar
- **Then** `approver` lo ejecuta, comprueba la salud de la app y publica el resultado

### Scenario: una aprobación no autorizada, caducada o repetida no ejecuta nada
- **Given** una propuesta pendiente
- **When** la aprueba un usuario que no está en `approvers`, o pasan más de 10 minutos, o se pulsa por segunda vez
- **Then** no se ejecuta nada y queda registrado

### Scenario: una propuesta inválida no llega a Slack
- **Given** el agente envía al socket un verbo que no existe en el fichero de la app
- **When** `approver` lo recibe
- **Then** lo rechaza sin publicar botones

### Scenario: el límite de acciones bloquea
- **Given** 3 acciones de escritura en `delta` en la última hora
- **When** se propone otra
- **Then** el bot la bloquea y pide intervención manual

### Scenario: el repo público no filtra la infraestructura
- **Given** el repo público
- **When** pasa la CI
- **Then** `gitleaks` no encuentra secretos y no hay datos reales de la infraestructura

## Notas técnicas / riesgos

### Flujo
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
approver (CT rescue, usuario `approver`; es otra app de Slack)
   │ 5. publica en #rescue: "¿Ejecutar restart en delta?" [Aprobar] [Rechazar]
   │ 6. una persona autorizada pulsa Aprobar
   │ 7. ejecuta con la clave de ESCRITURA ──SSH/Tailscale──► rescue-shell en la VM
   ▼
#rescue: resultado y comprobación de salud posterior
```

Las alertas entran por las integraciones de Slack que ya traen Healthchecks y
Uptime Kuma. Así **no hace falta ningún endpoint expuesto a Internet**.

### Componentes
| Componente | Dónde | Qué es |
|---|---|---|
| `rescue-shell` | Cada VM, en `/usr/local/sbin/` | Script que recibe todo comando que llega por SSH con las claves del bot. Solo acepta los verbos de una lista local de la VM (ver «En la VM») |
| `actions.conf` | Cada VM, en `/etc/rescue/`, propiedad de root y en modo 0644 | El catálogo cerrado de verbos de esa VM y el comando real de cada uno |
| `enroll` | Repo, en `bin/enroll` | Da de alta una VM a partir de su fichero de app. Lo ejecuta una persona con credenciales de administrador, una sola vez por VM |
| Hermes Agent | CT `rescue`, usuario `agent` | El motor: conversación en Slack y diagnóstico. Configuración y skills en `engines/hermes/` |
| `approver` | CT `rescue`, usuario `approver` | Servicio pequeño. Recibe propuestas del agente por un socket local, publica los botones, comprueba quién aprueba y ejecuta |
| Configuración privada | Fuera del repo público | Ficheros de cada app, mapa de accesos y secretos (ver «Configuración y secretos») |

**Dónde corre:** un contenedor LXC sin privilegios en **devsrv02**, para que el
rescate siga disponible si cae devsrv, donde están las apps.

### Modelo de seguridad
Dos ideas sostienen todo el modelo:

1. **El catálogo de acciones lo controla cada VM, no el bot.** Comprometer el bot
   no amplía lo que se puede hacer en una VM.
2. **Quien razona no tiene permiso para cambiar nada.** El agente (el modelo de
   lenguaje) solo tiene acceso de lectura. Las acciones que cambian cosas las
   ejecuta otro proceso, con otro usuario y otra clave, y solo tras un clic humano.

#### En la VM

- **Usuario `rescue`:** sin contraseña y sin shell interactiva. Tiene dos claves en
  su `authorized_keys`, ambas con
  `restrict,from="<IP de Tailscale del CT rescue>",command="/usr/local/sbin/rescue-shell <ro|rw>"`.
  - La clave de **solo lectura** pasa el argumento `ro`: `rescue-shell` solo
    acepta los verbos marcados `ro`.
  - La clave de **escritura** pasa el argumento `rw`: acepta además los verbos
    marcados `rw`.
- **`rescue-shell`:**
  - Lee `SSH_ORIGINAL_COMMAND` y lo trata como `<verbo>`. No admite argumentos
    libres ni metacaracteres de shell.
  - Busca el verbo en `/etc/rescue/actions.conf` y ejecuta el comando fijo que
    tiene asignado, sin pasar por `sh -c` con texto que llegue de fuera.
  - Rechaza cualquier verbo desconocido, o de escritura usado con la clave `ro`.
  - Registra en syslog cada llamada: verbo, clave, resultado y duración.
  - Limita el tamaño de la salida (por ejemplo, 64 KiB) y el tiempo de ejecución
    (por ejemplo, 120 s).
- **`sudoers`:** solo las líneas exactas que necesiten los comandos de
  `actions.conf`, sin comodines.
- **Qué no pasa nunca:** `actions.conf` no se descarga del bot ni se modifica desde
  él. Solo lo escribe `enroll`, ejecutado por una persona.

#### En el CT `rescue`

- **Contenedor:** LXC sin privilegios, con Debian 13 y actualizaciones de seguridad
  automáticas.
- **Usuarios Unix separados:**
  - `agent` ejecuta Hermes. Puede leer: la clave SSH de solo lectura, los tokens
    de la app de Slack del agente, la clave del proveedor del modelo y los
    ficheros de las apps (sin secretos).
  - `approver` ejecuta el servicio de aprobaciones. Puede leer: la clave SSH de
    escritura y los tokens de la app de Slack de aprobaciones.
  - Ningún usuario puede leer los ficheros del otro (modo 0600 y propietarios
    distintos).
- **Comunicación entre los dos:** el agente solo habla con `approver` a través de
  un socket Unix (`/run/rescue/approver.sock`) escribible por el grupo `agent`.
  Únicamente puede enviar `{app, verb, motivo}`. `approver` valida que `app` y
  `verb` existan en la configuración y que el verbo sea `rw`; si no, lo rechaza.
- **Hermes:** la ejecución de comandos local queda desactivada o limitada a un
  directorio de trabajo vacío. Las herramientas del agente son solo las skills de
  `engines/hermes/`:
  - `diagnose(app, verb)`, que usa la clave de solo lectura;
  - `request_action(app, verb, motivo)`, que escribe en el socket;
  - `health(app)`, que hace un GET HTTP a la URL de salud declarada.

#### Slack

- **Dos apps de Slack:** `rescue` para la conversación y `rescue-approvals` para
  los botones. Las dos usan **Socket Mode**: sin endpoint público.
- **Canal:** `#rescue`, privado.
- **Quién puede aprobar:** solo los usuarios de la lista `approvers` de la
  configuración, identificados por su ID de usuario de Slack, nunca por nombre.
  Si pulsa otra persona, no se ejecuta nada y se registra el intento.
- **Validez de una aprobación:**
  - es de un solo uso;
  - está ligada a `{app, verb, id de propuesta}`;
  - caduca a los 10 minutos.
- **Límites:** como máximo una acción de escritura por app cada 5 minutos, y como
  máximo 3 por hora. Pasado eso, el bot pide intervención manual.

#### Datos que salen hacia el modelo

- Los logs y salidas de diagnóstico se envían al proveedor del modelo de lenguaje.
  Se aplica una redacción básica antes de enviarlos: patrones de tokens, claves
  privadas y `password=`. No es completa, y la spec lo asume.
- Todo lo que devuelven las VMs se trata como **dato no fiable**. El prompt del
  sistema lo marca como tal, aunque la garantía real son las capas «En la VM» y «En el CT `rescue`»,
  no el prompt.
- El proveedor y el modelo se eligen en la configuración. Para apps con datos
  sensibles, la opción prevista es un modelo local.

#### Red

- **Tailscale:** el CT `rescue` es un nodo con la etiqueta `tag:rescue`. La política
  del tailnet solo le permite `tag:rescue → tag:app:22`. No tiene acceso a Proxmox
  (8006/8007) ni al resto de la LAN.
- **Ningún puerto entrante** desde Internet.

#### Registro

- `approver` escribe en `/var/log/rescue/audit.jsonl` cada propuesta, aprobación,
  rechazo, ejecución, resultado y salud posterior, con el ID de Slack de quien
  aprobó.
- `rescue-shell` deja en el syslog de cada VM el registro del lado del destino.

#### Repo abierto

- El repo público no contiene IPs, hosts, rutas reales, IDs de Slack ni claves.
  Solo código, esquemas y ejemplos con datos ficticios.
- La CI ejecuta `gitleaks` en cada PR.

### Formatos
#### Fichero de una app (`apps/<app>.yaml`, en la configuración privada)

```yaml
name: delta
url: https://delta.autarqui.co
health: { url: https://delta.autarqui.co/healthz, expect: 200 }
ssh: { host: delta.tailnet-xyz.ts.net, user: rescue }   # nombre MagicDNS de Tailscale
actions:                     # verbo → modo; el comando real solo vive en la VM
  status: ro
  logs: ro
  resources: ro
  restart: rw
  rollback: rw
runbook: runbooks/delta.md   # opcional: fallos conocidos de la app
```

#### `actions.conf` (en la VM, lo escribe `enroll`)

```
# verbo     modo  comando
status      ro    systemctl status --no-pager delta.service
logs        ro    journalctl -u delta.service -n 200 --no-pager
resources   ro    /usr/local/lib/rescue/resources.sh
restart     rw    sudo systemctl restart delta.service
rollback    rw    sudo /srv/delta/bin/rollback-to-last-good
```

#### Contrato de rollback (lo implementa cada app, no el bot)

- Al terminar un despliegue, cuando la comprobación de salud pasa, el CI o
  despliegue de la app guarda la versión desplegada (etiqueta, imagen o commit) en
  `/srv/<app>/.last-good`.
- `rollback-to-last-good` vuelve a esa versión y devuelve 0 si después la app pasa
  su comprobación de salud.
- Una app sin este contrato simplemente no declara `rollback`.

### Configuración y secretos
- **Dónde vive:** repo privado `rescue-bot-config` (nombre provisional) con:
  `apps/`, `runbooks/`, `approvers.yaml` y `hermes.yaml` (proveedor y modelo).
- **Secretos:** se dejan como ficheros en el CT, nunca en git. Son las dos claves
  SSH, los tokens de las dos apps de Slack y la clave del proveedor del modelo.
- **Añadir una app:**
  1. crear `apps/<app>.yaml`;
  2. ejecutar `bin/enroll <app>` con credenciales de administrador;
  3. reiniciar el bot.

### Fase 0: spike de Hermes Agent (condición para seguir)
Lo que hay que confirmar antes de construir sobre Hermes:

1. Una skill propia puede ejecutar SSH con una clave concreta contra varios hosts
   (uno por app), sin depender de un único backend de terminal SSH.
2. Se puede desactivar o encerrar la ejecución local de comandos del agente.
3. Funciona en Slack con Socket Mode en un canal privado y responde a menciones.
4. Se puede cambiar de proveedor de modelo solo con configuración (al menos
   Anthropic y uno compatible con la API de OpenAI).

Si falla 1 o 2, el plan B es sustituir el motor por un bot propio mínimo con Slack
Bolt y un SDK compatible con varios modelos. Ese bot mantendría `rescue-shell`,
`approver` y los formatos de «Formatos» sin cambios.

### Preguntas abiertas
- Nombre del repo privado de configuración (el público es `autarquico/rescue-bot`).
- Proveedor y modelo por defecto.
- ¿Se publica Uptime Kuma (todavía no está instalado) antes que el bot, o se
  empieza solo con Healthchecks?
- Cómo se despliega hoy cada app. Es necesario para saber cuáles pueden declarar
  `rollback` desde el principio.

## Tasks

Notas no autoritativas. El estado vivo es el board.

- [ ] _pendiente_

## Referencias

_Sin contenido extraído del fuente._
