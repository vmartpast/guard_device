# GUARD — bitacora tecnica de plataforma

Registro de hallazgos experimentales del vertical de hardware/integracion.
Cada entrada recoge un comportamiento observado, su causa y su implicacion
de diseno.

---

## 2026-09-10 — Los drop-ins del fabricante sobrescriben la configuracion base

**Observado.** Dos configuraciones aplicadas editando el fichero principal
no tuvieron efecto:

- `RuntimeWatchdogSec` en `/etc/systemd/system.conf` — pisado por
  `/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`
- `Storage=persistent` en `/etc/systemd/journald.conf` — pisado por
  `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`

**Causa.** Raspberry Pi OS aplica su politica mediante drop-ins en
`*.conf.d/`, que tienen prioridad sobre el fichero de configuracion
principal con independencia de si estan en `/etc` o en `/usr/lib`.

**Implicacion.** Todo endurecimiento del sistema se realiza con drop-ins
propios en `/etc/systemd/*.conf.d/` (prefijo `50-guard-`) y se verifica
con `systemctl show`, nunca editando el fichero base. Verificar la
configuracion efectiva, no la escrita.

---

## 2026-09-10 — Sin RTC no hay marcas de tiempo fiables en arranque

**Observado.** Los cuatro arranques registrados presentan intervalos
solapados: el boot -1 declara comenzar a las 20:38:12 cuando el boot -2
seguia activo hasta las 20:38:57. Fisicamente imposible.

En una instalacion limpia, el primer arranque se fecho el 18 de junio
estando el sistema a 10 de septiembre — la fecha heredada del `mtime` de
la imagen.

**Causa.** La Raspberry Pi 4 carece de reloj de tiempo real. Al arrancar
adopta una hora estimada y NTP la corrige segundos despues, cuando el
journal ya ha escrito entradas con la marca falsa.

**Implicacion.**

1. Las metricas de arranque y recuperacion se miden con reloj externo.
   Los 51 s registrados proceden de cronometro en equipo remoto.
2. El campo `ts` de los eventos de deteccion no es fiable sin red.
   En operacion RF-silente no hay NTP disponible.
3. Se propone RTC por I2C (DS3231) como requisito derivado de la
   operacion sin red. Coste marginal frente a perdida de trazabilidad
   temporal de las detecciones.

---

## 2026-09-10 — Recuperacion automatica ante cuelgue del kernel

**Prueba.** Panic deliberado via `echo c > /proc/sysrq-trigger`.

**Resultado.** Recuperacion completa sin intervencion en **51 s**,
medidos desde equipo externo hasta respuesta a ICMP.

Desglose: ~20 s de deteccion del watchdog + 23,7 s de arranque
(2,5 s kernel + 21,2 s userspace) + margen de red.

**Evidencia.** Los mensajes del panic no alcanzan el journal: el kernel
muere antes del volcado. La traza indirecta es
`File .../system.journal corrupted or uncleanly shut down`, ausente en
los arranques terminados con apagado ordenado.

`bootstatus` permanece a `0` tras el reinicio: el driver del BCM2835 en
Pi 4 no expone el bit de causa. No es indicativo de fallo del watchdog.

**Implicacion.** Punto 4 del bloque 1.4 validado. El journal persistente
es requisito previo de esta medida — sin el, un reinicio borra toda
evidencia del fallo que lo provoco.

---

## 2026-09-10 — `ServiceWatchdogs` no es clave de `system.conf`

**Observado.** El drop-in generaba en cada arranque
`Unknown key 'ServiceWatchdogs' in section [Manager], ignoring`, mientras
que `systemctl show` devolvia `ServiceWatchdogs=yes`.

**Causa.** La clave no existe en `system.conf`; se gestiona en tiempo de
ejecucion con `systemctl service-watchdogs`. El valor mostrado era el
predeterminado, no el configurado: la verificacion daba correcto por
coincidencia.

**Implicacion.** `systemctl show` confirma el valor efectivo pero no que
proceda de la configuracion propia. Revisar el journal en busca de claves
ignoradas tras cada cambio.

---

## 2026-09-10 — Linea base de arranque

`systemd-analyze`: **23,654 s** (2,495 s kernel + 21,159 s userspace),
`multi-user.target` a los 15,008 s.

Cadena critica — candidatos a eliminacion en dispositivo sin red:

| Unidad | Coste | Observacion |
|---|---|---|
| `NetworkManager.service` | 7,697 s | Un tercio del arranque |
| `cloud-init-main.service` | 1,889 s | Aprovisionamiento cloud, sin uso |
| `cloud-init-local.service` | 0,391 s | idem |
| `cloud-init-network.service` | 0,153 s | idem |

**Implicacion.** Margen amplio de reduccion. Metrica "tiempo de arranque
hasta operativo" de la seccion 7.4: valor de partida documentado para
contrastar tras la optimizacion.

---

## 2026-09-10 — Reduccion del tiempo de arranque: cloud-init

**Accion.** Deshabilitado `cloud-init` mediante fichero centinela
`/etc/cloud/cloud-init.disabled`. El aprovisionamiento del dispositivo se
realiza con los scripts versionados de este repositorio, no con fuentes
de datos cloud.

**Resultado.**

| Metrica | Antes | Despues | Delta |
|---|---|---|---|
| Total | 23,654 s | 15,620 s | −8,03 s (−34 %) |
| Kernel | 2,495 s | 2,336 s | −0,16 s |
| Userspace | 21,159 s | 13,284 s | −7,88 s |
| `multi-user.target` | 15,008 s | 13,282 s | −1,73 s |

La mejora supera el coste directo de las unidades de `cloud-init`
(~2,4 s en cadena critica): al eliminar sus generadores desaparecen
tambien dependencias de ordenacion que retrasaban `sysinit.target`.

**Pendiente.** `NetworkManager` sigue siendo el mayor consumidor
(7,912 s, la mitad del arranque). No se modifica todavia: es la unica via
de administracion disponible. Se abordara cuando exista canal alternativo
(Ethernet o consola via ESP32).

---

## 2026-09-10 — Watchdog de servicio validado (bloque 1.4, punto 2)

**Implementacion.** `guard-detector-stub.service` con `Type=notify` y
`WatchdogSec=30s`. El stub notifica a systemd via `NOTIFY_SOCKET`
(`READY=1` al arrancar, `WATCHDOG=1` cada 5 s) y mantiene en paralelo el
fichero heartbeat `/run/guard/detector.health` de la seccion 3.3. Ambos
mecanismos de la especificacion quedan asi ejercitados.

`sd_notify` se implementa sobre socket UNIX en la biblioteca estandar,
sin dependencia de `python3-systemd`: el detector real no deberia
requerir paquetes adicionales para integrarse.

**Prueba A — muerte del proceso.** `kill -9` sobre el PID principal.
Reinicio automatico, `NRestarts` a 1. Valida `Restart=on-failure`.

**Prueba B — proceso vivo pero colgado.** `kill -STOP`: el proceso existe
pero deja de notificar, escenario equivalente a un detector bloqueado que
`Restart=on-failure` por si solo no detectaria.

| Instante | Evento |
|---|---|
| 21:02:41 | SIGSTOP sobre PID 2308 |
| 21:03:07 | `Watchdog timeout (limit 30s)!` → SIGABRT |
| 21:03:07 | `Failed with result 'watchdog'` |
| 21:03:12 | Reinicio (`RestartSec=5s`), nuevo PID 2383 |

Recuperacion total: **31 s** sin intervencion.

**Implicacion.** La plataforma detecta y recupera dos clases distintas de
fallo del detector: terminacion abrupta y bloqueo silencioso. El segundo
caso es el relevante en operacion — un pipeline de inferencia puede
quedar bloqueado sin morir, y sin watchdog de servicio permaneceria
"activo" indefinidamente sin producir detecciones.
