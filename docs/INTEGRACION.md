# GUARD — Plan de integración y especificación de plataforma

> **Ubicación en el repo:** `/opt/guard_device/docs/INTEGRACION.md`
> **Estado:** Borrador v0.3 — pendiente de visto bueno del director (F. Barranco)
> **Autor:** Vicente (vertical hardware/integración)
> **Última actualización:** 2026-09-10

---

## 1. Contexto y decisión de alcance

El vertical de procesado de señal (detección de ráfagas FHSS con YOLOv8n sobre
espectrogramas + clasificación con ResNet18) fue desarrollado y defendido por
Eduardo Mateos (TFG, junio 2026). Eduardo ya no participa activamente en el
proyecto, por lo que **no existe (ni existirá a corto plazo) un paquete
instalable de detección** listo para desplegar.

**Decisión:** el software de detección se trata como **artefacto externo de caja
negra**. Este TFG entrega la **plataforma** capaz de alojarlo, no la detección
funcionando de punta a punta. En consecuencia:

- El "contrato de interfaz" pasa a ser una **especificación unilateral de
  plataforma** (sección 3): condiciones que debe cumplir cualquier software de
  detección para ejecutarse en el dispositivo GUARD.
- La integración se desarrolla y valida contra un **servicio stub** (sección 4)
  que emite eventos conformes a la especificación.
- La viabilidad computacional se mide con un **benchmark de caja negra**
  (sección 5), sin evaluar calidad de detección.
- La adaptación USRP→HackRF de la cadena de señal y su validación quedan
  **explícitamente fuera de alcance** (sección 6).

### Reparto de responsabilidades

| Área | Responsable | Estado |
|---|---|---|
| Pipeline de detección (modelos, umbrales, espectrogramas) | Vertical de procesado (E. Mateos, cerrado) | Caja negra |
| Plataforma embebida, provisioning, servicios systemd | Este TFG | En curso |
| Interfaz física (ESP32, OLED, buzzer, LEDs, botones) | Este TFG | Pendiente |
| Energía (batería/solar), watchdog, autostart | Este TFG | Bloque 1.4 (3 de 4 puntos) |
| Encapsulado (CAD, impresión 3D, IP54) | Este TFG | Horizonte |
| Adaptación y validación HackRF en cadena de señal | Fuera de alcance | Documentado como limitación |

---

## 2. Estado actual del sistema

- **Hardware:** Raspberry Pi 4 Model B (2 GB), HackRF One, ESP32 (UART),
  OLED SSD1306, buzzer, LEDs, botones, alimentación solar/batería.
- **SO:** Raspberry Pi OS Lite 64-bit, Debian 13 Trixie, kernel 6.18.
- **Acceso remoto:** VSCode Remote SSH sobre red local / Tailscale, usuario
  `guard`. Repositorio en GitHub (`vmartpast/guard_device`).

### 2.1 Provisioning implementado

Todos los scripts son idempotentes y residen en `system/os-setup/`:

| Script | Función | Estado |
|---|---|---|
| `01-base.sh` | Backup de `/boot/firmware`, journal persistente, `gpu_mem=16`, Bluetooth y avahi deshabilitados | ✅ |
| `02-watchdog.sh` | Watchdog hardware BCM2835 explícito vía drop-in (20 s) | ✅ |
| `03-boot-trim.sh` | `cloud-init` deshabilitado (reducción de arranque) | ✅ |

**Servicios propios:**

| Unidad | Función | Estado |
|---|---|---|
| `guard-detector-stub.service` | Stub del detector, `Type=notify`, `WatchdogSec=30s` | ✅ |

### 2.2 Nota de corrección respecto a v0.2

La versión anterior de este documento daba por completados un script
`silent_mode` y una unidad `guard-silent-mode.service`. **Ninguno de los dos
existía en el sistema**: no había fichero, unidad ni rastro en el historial del
editor. Se retiran del estado y pasan a trabajo pendiente (§7.2, punto 3).

### 2.3 Justificación del sistema operativo

**Decisión:** Raspberry Pi OS Lite 64-bit (base Debian 13 Trixie).

Criterios: en una plataforma de 2 GB de RAM, cada MB consumido por el SO se
resta del presupuesto del detector (§3.4), por lo que la huella base es el
criterio dominante junto al soporte del hardware.

| Alternativa | Valoración | Motivo de descarte / elección |
|---|---|---|
| **Raspberry Pi OS Lite 64-bit** | ✅ Elegida | Soporte oficial del fabricante para el hardware exacto (firmware, watchdog BCM2835, overlays UART); base Debian estable con ciclo de seguridad largo; repositorios arm64 completos (Python, ONNX Runtime); huella mínima sin entorno gráfico; el ajuste fino se realiza de forma reproducible y auditable vía los scripts de §2.1 |
| Ubuntu Server 24.04 arm64 | ❌ | Mayor consumo base de RAM (snapd, cloud-init); incompatible con el presupuesto de recursos en 2 GB sin ganancia funcional |
| DietPi | ❌ | Capa de scripts sobre el mismo Debian; menor trazabilidad y garantías; su ahorro ya se obtiene manualmente con el provisioning propio, que además es documentable |
| Yocto / Buildroot | ⏭ Línea futura | Respuesta canónica para producto embebido (imagen mínima inmutable, superficie de ataque reducida), pero coste de entrada desproporcionado para el alcance del TFG. Se documenta como trabajo futuro: "migración a imagen Yocto minimal para endurecimiento y despliegue en producción" |

**Consumo medido en reposo:** ~150–200 MB (sistema base, sin servidor de
desarrollo conectado). El presupuesto de §3.4 se sostiene.

---

## 3. Especificación de interfaz de plataforma (v0.1)

Cualquier software de detección que se despliegue en el dispositivo GUARD debe
cumplir lo siguiente. Los valores marcados `[TBD]` se fijarán tras el benchmark.

### 3.1 Empaquetado y arranque

- Se instala bajo `/opt/guard_detector/` con su propio virtualenv o binario
  autocontenido. No modifica ficheros fuera de su directorio.
- Se ejecuta como servicio systemd **`guard-detector.service`**, usuario no
  privilegiado `guard`, `Restart=on-failure`.
- Debe arrancar sin red (operación RF-silente): sin descargas de modelos ni
  telemetría en tiempo de ejecución.

### 3.2 Salida de eventos (JSON Lines)

Eventos por stdout (capturados por journald) y opcionalmente en socket UNIX
`/run/guard/detector.sock`. Un objeto JSON por línea:

```json
{
  "ts": "2026-09-09T12:34:56.789Z",
  "type": "detection",
  "confirmed": true,
  "label": "drone",
  "model": "dji_mini3",
  "confidence": 0.91,
  "window_s": 0.1,
  "bursts": 14,
  "rssi_dbfs": -42.5
}
```

- `type`: `detection` | `status` | `error`.
- `confirmed`: resultado de la agregación por ventana (criterio del pipeline:
  umbral de confianza + K ráfagas mínimas por ventana; los valores concretos
  son internos al detector, la plataforma solo consume el booleano).
- `model` y `rssi_dbfs` son opcionales (`null` si no disponibles).
- La plataforma (servicio puente → ESP32) reacciona **solo** a
  `type=detection && confirmed=true`.

> **Limitación conocida:** el campo `ts` no es fiable en operación sin red.
> Véase §9.1.

### 3.3 Health check

- Fichero heartbeat `/run/guard/detector.health` tocado al menos cada 10 s
  mientras el pipeline está vivo, **o** systemd `WatchdogSec=` con `sd_notify`.
- La plataforma considera el detector caído si el heartbeat supera 30 s de
  antigüedad y lo refleja en la UI (estado degradado en OLED, LED de fallo).

Ambos mecanismos están implementados y validados en el stub (§4).

### 3.4 Presupuesto de recursos (Pi 4, 2 GB)

| Recurso | Límite | Mecanismo |
|---|---|---|
| RAM (RSS) | **1200 MB máx.** `[validar en benchmark]` | `MemoryMax=` en la unidad |
| CPU | 3 de 4 núcleos sostenidos | `CPUQuota=300%` |
| Almacenamiento | 2 GB en `/opt/guard_detector` | convención |
| Temperatura | sin throttling sostenido a 25 °C ambiente | benchmark térmico |

Presupuesto reservado para plataforma: SO + servicios propios ≈ 500–600 MB;
margen de seguridad ≈ 200 MB.

> **Nota de medición:** el servidor de VSCode Remote consume ~1 GB de RAM
> mientras está conectado. Toda medida de consumo debe tomarse sobre sesión SSH
> plana, no desde el entorno de desarrollo.

### 3.5 Definición de evento de alerta (plataforma)

La plataforma genera **una alerta física** (buzzer + LED + OLED) cuando recibe
una detección confirmada, con histéresis: no se re-alerta por detecciones del
mismo episodio dentro de una ventana de `[TBD, propuesta: 10 s]`. El tono/patrón
puede modularse con `rssi_dbfs` si está disponible (requisito ET: tono variable
según potencia para no depender de la pantalla).

---

## 4. Servicio stub: `guard-detector-stub` ✅

Implementación mínima de la especificación §3, usada para desarrollar y
demostrar toda la vertical de plataforma sin el detector real.

**Ubicación:** `detector-stub/guard_detector_stub.py`. Python 3, solo biblioteca
estándar. `sd_notify` implementado sobre socket UNIX para no requerir
`python3-systemd` — el detector real tampoco debería necesitarlo.

**Modos de operación** (`--mode`):

| Modo | Comportamiento | Valida | Estado |
|---|---|---|---|
| `idle` | Solo `status` + heartbeat | arranque, health check, UI en reposo | ✅ |
| `sporadic` | Detección confirmada cada 30–120 s (aleatorio) | cadena alerta completa Pi→UART→ESP32→buzzer/OLED | ✅ |
| `burst` | Ráfaga de 4–9 detecciones en pocos segundos | histéresis de alerta (§3.5) | ✅ |
| `flaky` | Bloqueo silencioso o salida con código 1 | watchdog de servicio, `Restart=`, estado degradado en UI | ✅ |
| `load` | Reserva RAM y satura CPU (parametrizable) | presupuesto de recursos, térmica, estabilidad | ✅ |

El modo `flaky` alterna dos clases de fallo deliberadamente distintas:
terminación con error (detectable por `Restart=on-failure`) y bloqueo silencioso
con el proceso vivo (solo detectable por `WatchdogSec=`). El segundo es el caso
relevante en operación: un pipeline de inferencia puede quedar bloqueado sin
morir.

**Criterio de éxito del demostrador:** el dispositivo completo (Pi + ESP32 +
UI física) funciona de punta a punta contra el stub en todos los modos,
incluyendo recuperación automática en `flaky`.

---

## 5. Benchmark de caja negra (viabilidad en 2 GB) ⬜

Objetivo: medir si la clase de carga del pipeline (inferencia YOLOv8n sobre
imágenes 640×640) **cabe** en la Pi 4/2 GB y a qué coste. No se evalúa calidad
de detección.

- **Artefactos:** pesos reales de Eduardo si el director los facilita; en su
  defecto, YOLOv8n genérico de Ultralytics (carga computacional equivalente).
- **Runtimes a comparar:** PyTorch CPU (referencia) vs export a **ONNX Runtime**
  y **NCNN** (esperablemente los únicos viables en 2 GB).
- **Métricas:** latencia por imagen (p50/p95), RSS máximo, uso de CPU,
  temperatura y throttling en ejecución sostenida (≥30 min), tiempo de carga
  del modelo.
- **Criterio:** latencia media por ventana < duración de ventana equivalente
  para operación continua, o en su defecto documentar el factor de tiempo real
  alcanzable (p. ej. "procesa 1 de cada N ventanas").
- **Salida:** tabla comparativa para la memoria + valores definitivos de §3.4.

Riesgo conocido: el pipeline original corrió en RTX 4070 con procesado offline;
la brecha investigación→embebido es el núcleo del análisis de este TFG.

---

## 6. Fuera de alcance (documentado, no ocultado)

- **Adaptación USRP B210 → HackRF One de la cadena de captura** y validación de
  que la detección mantiene rendimiento con 8 bits de resolución. Se documenta
  el trade-off (8 vs 12 bits, ~340 € vs ~1200 €, requisito de bajo coste del
  ET) en el análisis de plataforma. El bring-up físico del HackRF (enumeración
  USB, consumo, térmica) **sí** es de este TFG; su uso en la cadena de señal, no.
- Modificación de modelos, umbrales o pipeline de espectrogramas.
- Reentrenamiento o evaluación de precisión de detección.

---

## 7. Plan de trabajo

### 7.1 Consolidación de carpetas ✅

Resuelto (2026-09-09). Canónica: `/opt/guard_device` (guion bajo).

La carpeta duplicada `/opt/guard-device` contenía el backup de `/boot/firmware`
generado por `01-base.sh`, que fue rescatado antes de eliminarla. La causa del
duplicado era una ruta con guion medio codificada en el propio script; corregida
en origen para impedir que vuelva a generarse.

### 7.2 Bloque 1.4 — Watchdog y autostart 🔶 (3 de 4)

1. **Watchdog hardware BCM2835** ✅ — drop-in propio
   `/etc/systemd/system.conf.d/50-guard-watchdog.conf` con
   `RuntimeWatchdogSec=20s`. Validado con kernel panic deliberado
   (`sysrq-trigger`): recuperación autónoma completa en **51 s**.
2. **Watchdog de servicio** ✅ — `WatchdogSec=30s` sobre
   `guard-detector-stub.service`. Validado con `kill -9` (reinicio inmediato) y
   con `kill -STOP` (bloqueo silencioso): detección y recuperación en **31 s**.
3. **Autostart: cadena de dependencias** ⬜ —
   `silent-mode → detector(-stub) → puente-UART` con `After=`/`Wants=`.
   **Bloqueado:** requiere `silent_mode`, que no existe (§2.2), y el puente
   UART, que requiere el ESP32.
4. **Prueba de recuperación en frío** ✅ — cubierta por la validación del punto 1.

### 7.3 Siguientes bloques

`silent_mode` → cadena de dependencias (§7.2.3) → protocolo UART Pi↔ESP32 →
firmware UI (OLED/buzzer/LEDs/botones) → benchmark (§5) → energía → encapsulado.

**Precaución en `silent_mode`:** desactiva la interfaz de red, que es la única
vía de administración disponible. No debe implementarse antes de disponer de
canal alternativo (§9.2).

### 7.4 Métricas de validación de plataforma (para la memoria)

| Métrica | Valor | Estado |
|---|---|---|
| Tiempo de arranque hasta operativo | **15,62 s** (2,34 kernel + 13,28 userspace) | Medido |
| Recuperación tras cuelgue del kernel (watchdog HW) | **51 s** | Medido |
| Recuperación tras bloqueo del detector (watchdog de servicio) | **31 s** | Medido |
| Consumo por modo (desarrollo / silente / carga) | — | Pendiente |
| Autonomía real con batería | — | Pendiente |
| Latencia detección→alerta física | — | Pendiente (requiere ESP32) |
| RSS/CPU/temperatura bajo carga sostenida | — | Pendiente (§5) |

**Optimización de arranque:** línea base inicial 23,65 s → 15,62 s (−34 %) tras
deshabilitar `cloud-init`. `NetworkManager` sigue siendo el mayor consumidor
(7,9 s, la mitad del arranque restante); su optimización queda supeditada a
disponer de canal de administración alternativo.

---

## 8. Registro de decisiones

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-09 | SO: Raspberry Pi OS Lite 64-bit (Debian 13) | Huella mínima en 2 GB, soporte oficial del HW, provisioning propio reproducible (§2.3) |
| 2026-09 | Detector = caja negra; especificación unilateral de plataforma | Vertical de procesado cerrado y sin continuidad |
| 2026-09 | Stub como pieza central de integración y demo | Desacopla desarrollo, demostrable en defensa |
| 2026-09 | Benchmark con YOLOv8n (real o genérico) en ONNX/NCNN | Carga equivalente sin depender de terceros |
| 2026-09 | HackRF: bring-up sí, cadena de señal no | Frontera de alcance del TFG |
| (previo) | `/opt/guard_device` canónica (guion bajo) | Consolidación de duplicado accidental |
| 2026-09-10 | Configuración del sistema mediante drop-ins propios en `/etc/systemd/*.conf.d/` con prefijo `50-guard-` | Raspberry Pi OS sobrescribe los ficheros de configuración base mediante sus propios drop-ins en `/usr/lib`. Verificar siempre configuración efectiva con `systemctl show`, no la escrita |
| 2026-09-10 | Journal persistente obligatorio (`Storage=persistent`) | Sin él, un reinicio por watchdog borra toda evidencia del fallo que lo provocó: imposibilita las métricas de §7.4 |
| 2026-09-10 | `cloud-init` deshabilitado | Aprovisionamiento cloud sin función en dispositivo embebido sin red; coste de 8 s de arranque |
| 2026-09-10 | Todo el trabajo versionado en repositorio remoto desde el primer commit | Pérdida del provisioning previo por corrupción del sistema de ficheros |
| 2026-09-10 | Propuesta: RTC DS3231 por I2C | Requisito derivado de la operación sin red (§9.1) |

---

## 9. Limitaciones de plataforma identificadas

### 9.1 Ausencia de reloj de tiempo real

La Raspberry Pi 4 carece de RTC. Al arrancar adopta una hora estimada y solo se
corrige cuando NTP dispone de red. En operación RF-silente **no hay red**, por
lo que el dispositivo opera permanentemente con hora no fiable.

**Evidencia observada:** en instalación limpia, el primer arranque se fechó casi
tres meses antes de la fecha real. Entre arranques sucesivos los intervalos
registrados llegan a solaparse, lo que hace imposible ordenar eventos por sus
propias marcas de tiempo.

**Consecuencias:**

1. El campo `ts` de los eventos de detección (§3.2) no es fiable sin red, lo que
   compromete la trazabilidad temporal de las detecciones.
2. Las métricas de arranque y recuperación de §7.4 deben medirse con reloj
   externo. Los valores de 51 s y 31 s proceden de cronometraje remoto.

**Mitigación propuesta:** módulo RTC DS3231 por I2C (bus disponible). Coste
marginal frente a la pérdida de trazabilidad.

### 9.2 Ausencia de canal de administración fuera de banda

El dispositivo se administra exclusivamente por red (SSH sobre WiFi). Un fallo
de la interfaz de red deja el sistema inaccesible aunque siga operativo,
situación verificada durante el desarrollo.

Esta limitación entra en conflicto directo con el requisito de operación
RF-silente, que exige desactivar la interfaz inalámbrica: la implementación de
`silent_mode` (§7.3) haría el dispositivo inaccesible por diseño.

**Mitigaciones a evaluar:**

- Consola serie sobre UART (disponible actualmente en `ttyS0`, pero ese puerto
  está reservado para el enlace con el ESP32).
- Interfaz Ethernet (adaptador USB), independiente del subsistema de radio.
- Consola de administración a través del propio ESP32, aprovechando el enlace
  ya previsto.

La decisión condiciona el diseño de la interfaz física y debe tomarse antes de
implementar `silent_mode`.
