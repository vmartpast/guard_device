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

---

## 2026-09-10 — Stub del detector completo (seccion 4)

`detector-stub/guard_detector_stub.py`, Python 3 sin dependencias
externas. Implementa la especificacion de plataforma de la seccion 3 y
los cinco modos de la seccion 4.

| Modo | Comportamiento | Valida |
|---|---|---|
| `idle` | status + heartbeat | arranque, health check, UI en reposo |
| `sporadic` | deteccion confirmada cada 30-120 s | cadena de alerta completa |
| `burst` | episodios de 4-9 detecciones en segundos | histeresis (seccion 3.5) |
| `flaky` | bloqueo silencioso o salida con codigo 1 | watchdog de servicio, `Restart=` |
| `load` | reserva RAM y satura CPU | presupuesto de recursos (seccion 3.4) |

**Decisiones de implementacion.**

- Solo biblioteca estandar. `sd_notify` sobre socket UNIX en lugar de
  `python3-systemd`: reduce lo que el detector real necesitaria instalar.
- Doble mecanismo de salud — fichero heartbeat y `WATCHDOG=1` — porque la
  seccion 3.3 admite ambos y conviene tener los dos ejercitados.
- `model` se emite `null` de forma aleatoria: la plataforma debe tolerar
  el campo opcional de la seccion 3.2.
- El modo `load` no reproduce la carga real de inferencia; ejercita los
  limites de la unidad, no la viabilidad computacional. Esa se mide en el
  benchmark de la seccion 5.

**Observacion menor.** En modo `flaky` el fallo se produce en el primer
multiplo del intervalo de heartbeat posterior a `--fail-after` (10 s para
un valor de 8 s). Irrelevante para su funcion; anotado por exactitud.

---

## 2026-09-11 — Enlace UART Pi↔ESP32 operativo

**Configuración.** `dtoverlay=uart3` en la Pi (GPIO 4 TX / GPIO 5 RX, puerto
`/dev/ttyAMA3`) contra UART2 remapeada en el ESP32 (GPIO 32 TX / GPIO 33 RX).

El puerto resultante es `/dev/ttyAMA3`, no `ttyAMA1`: el kernel numera según la
UART del SoC, no por orden de aparición.

**Hallazgo — los pines por defecto de UART2 no son utilizables.** La placa es una
Freenove ESP32-WROVER. En los módulos de la serie WROVER, GPIO16 y GPIO17 están
dedicados a la PSRAM interna y ni siquiera salen al conector; sin embargo, son
los pines por defecto de `Serial2` en Arduino-ESP32. Cablear según la
documentación genérica de ESP32 habría producido ausencia total de tráfico o,
peor, corrupción de PSRAM.

Pines descartados en esta placa y motivo: 16/17 (PSRAM), 13/14/15/2 (SDMMC),
12 (MTDI, strapping), 0/2 (Boot Mode), 34-39 (solo entrada, no pueden
transmitir). GPIO 32 y 33 son los únicos libres sin función reservada.

**Método de validación por fases.** Se verificó primero el puerto de la Pi de
forma aislada mediante loopback (puente entre pines 7 y 29,
`tools/uart_loopback_test.py`), y solo después se conectó el ESP32. Separar
ambas verificaciones evita depurar simultáneamente cuatro posibles causas
(overlay, cableado, firmware, permisos).

**Resultado.** Eco bidireccional confirmado: la Pi envía una cadena y recibe
`ECO:<cadena>` desde el ESP32.

**Nota para el parser.** `println` en Arduino termina las líneas con `\r\n`. El
extremo Pi debe tolerar el retorno de carro: de lo contrario quedaría incluido
en el cálculo del checksum y toda trama sería descartada.

---

## 2026-09-11 — Corrupción del repositorio local por apagados sucesivos

**Observado.** Tras varios ciclos de apagado para cablear el ESP32, el
repositorio local quedó inutilizable: cuatro objetos de `.git/objects` con
tamaño cero y `fatal: could not parse HEAD`. El fichero
`tools/uart_loopback_test.py` presentaba también 0 bytes en disco pese a
haberse escrito y ejecutado correctamente minutos antes.

**Causa.** Escrituras pendientes en la caché del sistema de ficheros que no
llegaron a la tarjeta SD antes del apagado. Los `.md` y los scripts de
`os-setup`, escritos con más antelación, sobrevivieron intactos.

**Recuperación.** Clonado limpio desde el remoto. El commit afectado ya estaba
publicado, por lo que la pérdida efectiva se limitó a una entrada de esta
bitácora aún sin subir.

**Implicaciones.**

1. `sync` explícito antes de cada apagado durante el trabajo de cableado. El
   `shutdown` ordenado no siempre completa el volcado con una SD lenta y
   escrituras recientes.
2. Confirma el valor del remoto como única copia fiable: el repositorio local
   reside en el mismo medio que está sujeto a los cortes.
3. El ciclo apagar–cablear–encender es intrínseco al trabajo de integración
   hardware. La fragilidad de la SD frente a cortes no es un incidente
   aislado sino una condición de trabajo, y refuerza el interés de un sistema
   de ficheros raíz en solo lectura para el dispositivo desplegado.

---

## 2026-09-11 — Interfaz física completa y validada

Cadena operativa de punta a punta con hardware real: detector (stub) →
journal → servicio puente → UART → ESP32 → LCD, LEDs y buzzer.

### Componentes

| Elemento | Ubicación | Función |
|---|---|---|
| `tools/guard_bridge.py` | Pi | Traduce eventos JSON a tramas UART, aplica histéresis, emite latido y telemetría |
| `esp32-firmware/guard_interfaz.ino` | ESP32 | Parser con checksum, máquina de estados, presentación en LCD/LED/buzzer |

La interfaz física cuelga íntegramente del ESP32 (decisión §2 del
protocolo). El microcontrolador mantiene su propio temporizador de enlace,
de modo que detecta la caída de la plataforma sin depender de que esta se
lo comunique.

### Pruebas superadas

**Histéresis (§3.5).** Stub en modo `burst`: un episodio de 6 detecciones
en pocos segundos produjo **una sola alerta**; el puente registró las 5
restantes como suprimidas. Sin histéresis el buzzer habría sonado seis
veces por el mismo objetivo.

**Pérdida de enlace con prioridad sobre alerta.** Desconexión en caliente
de la línea TX de la Pi durante una alerta activa: transición a
`ENLACE PERDIDO` en **~6 s**, coincidiendo con el umbral de diseño (tres
latidos perdidos). Al reconectar, retorno a `OPERATIVO` en ~2 s.

Este comportamiento implementa la decisión de diseño de §2: la interfaz
prefiere declarar que no tiene información fiable antes que continuar
mostrando datos antiguos, incluso a costa de interrumpir una alerta en
curso.

**Confirmación de alerta.** El ESP32 responde `ACK|DET` a cada detección
reenviada, lo que permitirá medir la latencia detección→alerta física
(métrica pendiente de §7.4).

### Incidencias resueltas

**Separador final en el parser.** El protocolo cierra la lista de campos
con `|` antes del checksum. El parser inicial no lo eliminaba, por lo que
el último campo arrastraba el separador (`"OK|"` en lugar de `"OK"`) y
toda trama `HB` se rechazaba con `ERR|FMT`. El checksum sí validaba, lo
que acotó el fallo al troceado y no a la integridad del enlace.

**Rango de tono limitado por el transductor.** El diseño inicial mapeaba
RSSI a 700–2600 Hz. Con RSSI bajo el tono resultante caía en torno a
700 Hz, frecuencia a la que el buzzer piezoeléctrico es prácticamente
inaudible pese a estar funcionando. El rango se estrechó a 1600–2600 Hz,
conservando la modulación por potencia dentro de la banda útil del
componente. El volumen adicional se obtuvo eliminando la resistencia en
serie.

El rango de tono no es, por tanto, una elección de diseño sino un
parámetro determinado por la respuesta en frecuencia del transductor, y
solo pudo fijarse midiendo sobre el componente real.

---

## 2026-09-11 — Etapa de conmutación para el buzzer

**Problema.** Con el buzzer atacado directamente desde el GPIO, el volumen
resultaba insuficiente para un aviso de campo. El pin entrega 3,3 V y su
capacidad de corriente está en el límite de lo que consume el transductor.

**Solución.** Etapa de conmutación con transistor bipolar NPN S8050:

| Conexión | Destino |
|---|---|
| GPIO 19 → resistencia 1 kΩ | base |
| Pin 5 V → pata + del buzzer | — |
| Pata − del buzzer | colector |
| Emisor | masa |

El GPIO pasa a conmutar el transistor en lugar de alimentar la carga; la
corriente procede del raíl de 5 V. El firmware no requiere cambio alguno:
sigue generando la misma señal sobre el mismo pin.

**Resultado.** Aumento de volumen apreciable, conservando la modulación de
tono por RSSI.

**Nota de montaje.** El patillaje del S8050 en encapsulado TO-92, con la
cara plana de frente y las patas hacia abajo, es emisor–base–colector de
izquierda a derecha. No es universal: el BC547, de encapsulado idéntico,
presenta el orden inverso (colector–base–emisor). Montado en espejo el
transistor no conduce y el fallo no produce ningún síntoma distinguible de
un buzzer averiado.

El diagnóstico se acotó puenteando la pata negativa del buzzer a masa
—descartando buzzer y alimentación— y forzando después la base a 5 V para
verificar la conducción del transistor por separado. Mismo método por
fases empleado con el enlace UART: aislar cada elemento antes de
combinarlos.

**Implicación de diseño.** Toda carga que supere unos pocos miliamperios
—buzzer, relés, iluminación— debe conmutarse, no alimentarse, desde un
GPIO. Criterio aplicable al resto de la interfaz física y al diseño
eléctrico del encapsulado.

---

## 2026-09-12 — Modo RF-silente y cadena de dependencias (bloque 1.4 completo)

### Canal de administración fuera de banda

Resuelta la limitación §9.2 de INTEGRACION.md. Consola serie sobre
`ttyAMA0` (GPIO 14/15, pines 8 y 10) con adaptador USB-TTL CH340G, jumper
de nivel lógico en 3,3 V.

Validado apagando la interfaz inalámbrica **desde la propia consola**: la
sesión sobrevive. Reproduce exactamente el escenario de `silent_mode` y
resuelve el incidente del 9 de septiembre, en el que el dispositivo quedó
operativo pero inaccesible.

Ventaja adicional sobre cualquier vía de red: la consola serie está
disponible durante el arranque del kernel, antes de que exista red.

### `guard-silent-mode`

Script reversible (`on` / `off` / `status`) instalado en
`/usr/local/sbin`, con unidad systemd asociada.

Desactiva lo que emite y delata posición: interfaces inalámbricas
(`rfkill`, `nmcli`) e indicadores luminosos de la placa. **No** apaga los
LEDs de la interfaz de operador: son información hacia el usuario, no
emisión hacia el exterior, y en operación silente son el único canal de
estado disponible al no haber red.

**Salvaguarda.** El script verifica que `ttyAMA0` existe y que su getty
está activo antes de proceder. Sin consola serie, aborta: activar el modo
silente dejaría el dispositivo inaccesible. La protección deriva
directamente del incidente del 9 de septiembre.

La cadena de detección no se ve afectada: el HackRF opera en recepción y
no emite.

### Cadena de dependencias (punto 3)

`guard-silent-mode` → `guard-detector-stub` → `guard-bridge`

Orden verificado en el journal de un arranque real. El modo silente se
establece **antes** de que arranque ningún otro servicio: el dispositivo
no emite en ningún momento de la secuencia.

Decisiones de acoplamiento:

- `guard-bridge` usa `Wants=` y no `Requires=` sobre el detector: debe
  seguir vivo aunque el detector caiga, porque es precisamente entonces
  cuando debe informar del estado degradado en la interfaz física.
- El detector usa solo `After=` sobre el modo silente, sin `Wants=`. Con
  `Wants=` el dispositivo arrancaba en silente en cada reinicio, lo que
  es correcto para despliegue pero inviable durante el desarrollo: cada
  reinicio dejaba el equipo sin red. Para despliegue basta
  `systemctl enable guard-silent-mode.service`; el `After=` garantiza el
  orden cuando esté activo.

### Estado del bloque 1.4

| Punto | Estado | Evidencia |
|---|---|---|
| 1. Watchdog hardware | ✅ | Kernel panic deliberado, recuperación en 51 s |
| 2. Watchdog de servicio | ✅ | `kill -9` y `kill -STOP`, recuperación en 31 s |
| 3. Cadena de dependencias | ✅ | Orden verificado en journal de arranque real |
| 4. Recuperación en frío | ✅ | Cubierto por la validación del punto 1 |

El dispositivo arranca desatendido, establece el silencio radioeléctrico,
levanta la detección y presenta estado en la interfaz física sin
intervención alguna.
