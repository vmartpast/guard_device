# GUARD — dispositivo embebido

Plataforma embebida para deteccion de UAVs de bajo coste mediante SDR.
TFG — vertical de hardware e integracion.

## Estructura

- `system/os-setup/` — scripts de provisioning del SO (idempotentes)
- `system/boot/` — copias de seguridad de `/boot/firmware`
- `system/udev/` — reglas udev (HackRF, ESP32)
- `tools/` — utilidades de diagnostico
- `reports/` — snapshots y resultados de benchmark
- `docs/` — documentacion de integracion
- `hardware/` — esquemas, CAD y fotografias

## Plataforma

- Raspberry Pi 4 Model B (2 GB)
- Raspberry Pi OS Lite 64-bit (Debian 13 Trixie)
- HackRF One, ESP32 (UART), OLED SSD1306
