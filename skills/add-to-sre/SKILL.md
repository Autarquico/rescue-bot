---
name: add-to-sre
description: Da de alta una app/VM en rescue-bot (el SRE de Slack). Úsala con "/add-to-sre" o cuando el usuario diga "añade X al SRE", "añade X al rescue-bot", "da de alta X en rescue".
---

# add-to-sre

Rutas: repo `${RESCUE_BOT_REPO:-~/src/autarqui.co/rescue-bot}`, configuración privada `${RESCUE_BOT_CONFIG:-~/src/autarqui.co/rescue-bot-config}`. Resuélvelas al principio y verifica que existen.

## Protocolo

1. **Datos básicos.** Pregunta (o deduce del contexto) el nombre de la app y el host (nombre MagicDNS de Tailscale). Si falta algo, pregunta una sola vez.
2. **Inspección de solo lectura** de la VM por SSH (nunca modifiques nada; sin `sudo` salvo que ya sea de lectura):
   - `systemctl list-units --type=service --state=running --no-pager`
   - `docker compose ls`
   - `ss -ltnp`
3. **Propón** el preset (unidad systemd o directorio compose) y la URL de salud (puerto en escucha + ruta probable, p. ej. `/healthz`). Pide confirmación de ambos.
4. **Rollback.** Comprueba si existe `/srv/<app>/bin/rollback-to-last-good` (`test -x` por SSH). Si existe, se declara `rollback`; si no, no, y dilo al usuario (referencia: contrato de rollback del README).
5. **Dry-run primero** (`--dry-run`: cero efectos; imprime la ficha YAML, las huellas de las host keys y el diff de enroll):
   ```
   <repo>/bin/add-app <app> --config <config-dir> <flags reunidos> --dry-run
   ```
   Consulta `bin/add-app --help` para los flags exactos. Muestra al usuario la salida completa.
6. **Aplica solo tras confirmación explícita** del usuario: repite el mismo comando sin `--dry-run`. Esa ejecución recarga el CT (`/usr/local/sbin/rescue-reload`) y verifica (`/usr/local/sbin/rescue-verify <app>`).
7. **Informa** del resultado: ficha creada, resultado de `rescue-verify` (`status` con `ro` ok, `restart` con `ro` rechazado), commit/push de la config y recarga del CT. Incluye enlaces markdown (commit o PR de la config, canal `#rescue`) cuando existan. Si algo falla, di qué paso y no reintentes a ciegas.

## Reglas

- No pegues secretos ni IDs reales en el repo público; todo dato real va a `<config-dir>`.
- No ejecutes `enroll` sin dry-run previo mostrado al usuario.
