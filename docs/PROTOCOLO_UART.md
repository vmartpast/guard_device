# GUARD — Protocolo de enlace Pi ↔ ESP32 (UART)

> **Ubicación en el repo:** `/opt/guard_device/docs/PROTOCOLO_UART.md`
> **Estado:** Borrador v0.1 — especificación de diseño, sin implementar
> **Autor:** Vicente (vertical hardware/integración)
> **Última actualización:** 2026-09-11

---

## 1. Propósito y alcance

Define el enlace serie entre la Raspberry Pi (plataforma de detección) y el
ESP32 (interfaz física: OLED SSD1306, buzzer, LEDs, botones).

El enlace transporta el estado del sistema y los eventos de detección hacia la
interfaz, y las pulsaciones de botón hacia la plataforma.

**Fuera de este documento:** el firmware del ESP32, el servicio puente en la Pi
y el cableado físico. Aquí solo se fija el contrato entre ambos.

---

## 2. Decisión de arquitectura: interfaz autónoma

**Toda la interfaz física cuelga del ESP32.** La Pi no controla directamente ni
la pantalla ni el buzzer ni los LEDs.

**Alternativa descartada:** conectar la OLED a la Pi por I2C y dejar al ESP32
solo el buzzer. Es más simple de implementar, pero si la Pi se cuelga la
pantalla queda congelada mostrando el último estado conocido — un operador
leería "sin detecciones" sin saber que el sistema está muerto.

Durante los ~51 s que tarda el dispositivo en recuperarse de un cuelgue
(medido, §7.4 de INTEGRACION.md), la interfaz debe informar del fallo, no
ocultarlo. Una interfaz que miente es peor que la ausencia de interfaz.

**Consecuencia de diseño:** el ESP32 mantiene su propio temporizador de enlace y
decide por sí mismo cuándo pasar a estado degradado. No depende de que la Pi se
lo indique — precisamente porque el caso a cubrir es que la Pi no pueda
indicarle nada.

---

## 3. Capa física

| Parámetro | Valor |
|---|---|
| Interfaz | UART asíncrona, 3,3 V |
| Velocidad | 115200 bps |
| Formato | 8N1 (8 bits de datos, sin paridad, 1 de parada) |
| Control de flujo | Ninguno |
| Señales | TX, RX, GND común |

**Advertencia de nivel lógico:** ambas placas operan a 3,3 V. No se requiere
adaptación de nivel, pero conectar cualquiera de las dos a un dispositivo de
5 V sin conversor destruiría el pin.

**Sin control de flujo** por simplicidad de cableado. La consecuencia es que
pueden perderse bytes, y de ahí la delimitación y el checksum de §4.

---

## 4. Formato de trama

```
<PREFIJO><TIPO>|<campo1>|<campo2>|...|*<CS>\n
```

| Elemento | Descripción |
|---|---|
| `PREFIJO` | `>` para Pi→ESP32, `<` para ESP32→Pi |
| `TIPO` | Identificador de 2–3 caracteres |
| `\|` | Separador de campos |
| `*` | Marca de inicio de checksum |
| `CS` | XOR de todos los bytes entre el prefijo y el `*`, en hexadecimal de 2 dígitos |
| `\n` | Fin de trama |

**Longitud máxima:** 80 bytes incluyendo terminador.

### 4.1 Justificación del formato

**Texto delimitado, no JSON ni binario.** El ESP32 tiene RAM para parsear JSON,
pero es lento y frágil ante tramas truncadas. Un formato binario es eficiente
pero indepurable: ante un fallo no se puede leer el tráfico directamente.

Con texto delimitado el parseo es un `strtok` y el enlace puede inspeccionarse
pinchando un adaptador USB-TTL y leyendo con cualquier terminal serie. En
depuración de campo esa propiedad vale más que los bytes ahorrados.

**Checksum XOR** en lugar de CRC: suficiente para detectar bytes perdidos o
corrompidos en un enlace corto punto a punto, y calculable en una línea en
ambos extremos.

### 4.2 Recuperación de sincronía

El receptor descarta bytes hasta encontrar un prefijo válido. Una trama con
checksum incorrecto se descarta silenciosamente en el ESP32 y genera `ERR` en
sentido contrario.

**Nunca se actúa sobre una trama no verificada.** Una trama `DET` corrupta que
disparase el buzzer sería una falsa alarma; descartarla solo retrasa la alerta
hasta la siguiente detección.

---

## 5. Tramas Pi → ESP32

### 5.1 `HB` — Latido

```
>HB|<uptime_s>|<estado>|*<CS>\n
```

| Campo | Tipo | Descripción |
|---|---|---|
| `uptime_s` | entero | Segundos desde el arranque del servicio puente |
| `estado` | enum | `OK` \| `DEGRADED` \| `ERROR` |

**Periodo: 2 s.** Es la trama que sostiene toda la arquitectura de §2: su
ausencia es lo que permite al ESP32 detectar que la plataforma ha caído.

`estado` refleja lo que la plataforma sabe del detector según §3.3 de
INTEGRACION.md:

- `OK` — heartbeat del detector fresco
- `DEGRADED` — heartbeat con más de 30 s de antigüedad
- `ERROR` — el servicio del detector no está activo

### 5.2 `DET` — Detección confirmada

```
>DET|<rssi_dbfs>|<model>|<conf>|*<CS>\n
```

| Campo | Tipo | Descripción |
|---|---|---|
| `rssi_dbfs` | decimal | Potencia recibida, o `-` si no disponible |
| `model` | cadena | Modelo estimado, o `-` si el detector no lo aporta |
| `conf` | decimal | Confianza `0.00`–`1.00` |

Se emite **solo** para `type=detection && confirmed=true` (§3.2 de
INTEGRACION.md).

**La histéresis se aplica en la Pi, no en el ESP32.** El servicio puente
implementa la ventana de §3.5 y no reenvía detecciones del mismo episodio. El
ESP32 alerta ante toda trama `DET` que reciba: mantiene el firmware simple y
deja la política en el lado donde es configurable.

`rssi_dbfs` permite modular el tono del buzzer según potencia (requisito ET:
indicación audible independiente de la pantalla).

### 5.3 `SYS` — Telemetría

```
>SYS|<cpu_pct>|<temp_c>|<mem_pct>|*<CS>\n
```

**Periodo: 10 s.** Para la pantalla de estado accesible por botón. No requiere
acción inmediata.

---

## 6. Tramas ESP32 → Pi

### 6.1 `BTN` — Pulsación

```
<BTN|<id>|<accion>|*<CS>\n
```

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | entero | Identificador del botón (0–n) |
| `accion` | enum | `SHORT` \| `LONG` |

El antirrebote se resuelve en el firmware del ESP32. La Pi recibe eventos ya
limpios.

### 6.2 `ACK` — Confirmación

```
<ACK|<tipo>|*<CS>\n
```

Solo para `DET`, permitiendo a la Pi registrar que la alerta física se emitió.
Sirve para medir la latencia detección→alerta (métrica de §7.4 de
INTEGRACION.md).

`HB` y `SYS` no se confirman: duplicarían el tráfico sin aportar información.

### 6.3 `ERR` — Trama inválida

```
<ERR|<codigo>|*<CS>\n
```

| Código | Significado |
|---|---|
| `CS` | Checksum incorrecto |
| `FMT` | Formato o número de campos inválido |
| `LEN` | Trama excede la longitud máxima |

Permite cuantificar la tasa de error del enlace en el journal de la Pi. Si
aparecen `ERR` con frecuencia, hay un problema físico (cableado, velocidad,
interferencia) que conviene detectar antes del despliegue.

---

## 7. Máquina de estados del ESP32

| Estado | Condición de entrada | Presentación |
|---|---|---|
| `BOOT` | Arranque del ESP32 | Pantalla de inicio, sin alertas |
| `LINK_DOWN` | Sin `HB` válido durante **6 s** | "ENLACE PERDIDO", LED rojo fijo |
| `IDLE` | `HB` con `estado=OK` | Estado normal, sin detecciones |
| `DEGRADED` | `HB` con `estado=DEGRADED` o `ERROR` | Aviso de detector caído, LED ámbar |
| `ALERT` | Recepción de `DET` | Buzzer, LED rojo intermitente, datos en pantalla |

### 7.1 Reglas de transición

- `ALERT` es transitorio: tras **10 s** sin nuevas `DET`, retorna al estado que
  indique el último `HB`.
- `LINK_DOWN` tiene **prioridad sobre todo**, incluido `ALERT`: si el enlace
  cae durante una alerta, la pantalla debe indicar que ya no hay información
  fiable.
- La salida de `LINK_DOWN` requiere una trama `HB` con checksum válido, no
  cualquier byte recibido.

### 7.2 Umbral de enlace perdido

**6 s = 3 heartbeats perdidos.** Un solo latido perdido puede deberse a ruido en
la línea; tres seguidos indican que la Pi no está transmitiendo.

El umbral es holgado frente a los 51 s de recuperación medidos: un cuelgue de la
Pi se refleja en la interfaz en 6 s y permanece visible durante los ~45 s
restantes de reinicio.

---

## 8. Asignación de puerto serie ✅

**Decidido (2026-09-11).** El inventario de UARTs de la Pi 4 permite atender
simultáneamente el enlace con el ESP32 y la consola de administración, sin
tener que sacrificar ninguna de las dos.

### 8.1 Inventario

| UART | GPIO | Estado | Asignación |
|---|---|---|---|
| PL011 principal (`ttyAMA0`) | 14/15 | Activa | **Consola de administración** |
| `uart2` | 0-3 | Disponible | Descartada: GPIO 0/1 son ID_SDA/ID_SCL (EEPROM de HAT) y GPIO 2/3 el I2C principal |
| `uart3` | 4-7 | Disponible | **Enlace con ESP32** |
| `uart4` | 8-11 | Disponible | Descartada: colisiona con CE0/CE1/MISO/MOSI del SPI |
| `uart5` | 12-15 | Disponible | Descartada: GPIO 14/15 es la UART principal |

La liberación de la PL011 en los pines principales es consecuencia de
`dtoverlay=disable-bt`, aplicado por `01-base.sh`: sin él, el Bluetooth ocupa
esa UART y la consola cae en la mini-UART, de menor calidad.

### 8.2 Configuración

El enlace con el ESP32 queda en `/dev/ttyAMA1`; la consola permanece en
`/dev/ttyAMA0` (`console=serial0,115200` en `cmdline.txt`).

### 8.3 Cableado

| Señal | Pi (GPIO) | Pi (pin físico) | ESP32 |
|---|---|---|---|
| Pi TX → ESP32 RX | GPIO 4 | 7 | RX |
| Pi RX ← ESP32 TX | GPIO 5 | 29 | TX |
| Masa común | GND | 9 | GND |

TX y RX se cruzan. Un cableado directo no produce error visible, solo ausencia
total de tráfico.

### 8.4 Resolución de la limitación §9.2 de INTEGRACION.md

La consola serie en `ttyAMA0` proporciona canal de administración fuera de
banda, independiente de la red y del subsistema de radio. Requiere únicamente
un adaptador USB-TTL en el lado del equipo de desarrollo.

Ventaja sobre Ethernet: la consola serie está disponible **durante el arranque**
y ante fallos que impidan levantar la red, escenario verificado durante el
desarrollo (2026-09-09, sistema operativo pero inaccesible).

Con ello `silent_mode` deja de estar bloqueado por la ausencia de canal
alternativo.

---

## 9. Verificación prevista

| Prueba | Método | Criterio |
|---|---|---|
| Integridad de trama | Inyección de tramas corruptas | Descarte + `ERR`, sin acción sobre la UI |
| Detección de enlace caído | Desconexión del cable TX | `LINK_DOWN` en ≤ 6 s |
| Recuperación de enlace | Reconexión | Retorno al estado indicado por `HB` |
| Cuelgue de la plataforma | `sysrq-trigger` en la Pi | `LINK_DOWN` durante el reinicio, retorno a `IDLE` al volver |
| Histéresis | Stub en modo `burst` | Una sola alerta por episodio |
| Estado degradado | Stub en modo `flaky` | Transición a `DEGRADED` |
| Latencia alerta | Marca temporal `DET` → `ACK` | Documentar valor (§7.4) |

Las pruebas de histéresis y estado degradado se ejecutan contra el stub (§4 de
INTEGRACION.md), sin necesidad del detector real.
