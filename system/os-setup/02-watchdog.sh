#!/usr/bin/env bash
# 02-watchdog.sh — GUARD: watchdog hardware (BCM2835) explicito y auditable.
#
# Raspberry Pi OS activa el watchdog via
# /usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf (60s/2min).
# Este script NO lo modifica: anade un drop-in propio en /etc, que tiene
# prioridad, para que la configuracion del dispositivo sea explicita,
# versionada y no dependa de valores por defecto del fabricante.
#
# Nota HW: el BCM2835 tiene un contador maximo de ~16 s. El nucleo de
# watchdog del kernel refresca el hardware por debajo mientras el
# temporizador logico configurado aqui siga vivo. Por eso 'timeout'
# reporta el valor logico y 'timeleft' el contador fisico real.

set -euo pipefail

DROPIN_DIR="/etc/systemd/system.conf.d"
DROPIN="${DROPIN_DIR}/50-guard-watchdog.conf"

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }
[[ -e /dev/watchdog0 ]] || { echo "ERROR: /dev/watchdog0 no existe" >&2; exit 1; }

echo "[*] Watchdog detectado: $(cat /sys/class/watchdog/watchdog0/identity)"

mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<'EOF'
# GUARD — politica de watchdog del dispositivo.
# Sustituye a 40-rpi-enable-watchdog.conf (prioridad de /etc sobre /usr/lib).
[Manager]
# Deteccion de cuelgue del sistema. systemd refresca a la mitad de este valor.
# 20s: compromiso entre reaccion en campo y falsos positivos bajo carga
# sostenida (revalidar tras el benchmark de la seccion 5).
RuntimeWatchdogSec=20s

# Si un apagado/reinicio ordenado se atasca, el HW fuerza el reinicio.
RebootWatchdogSec=60s

EOF

echo "[*] Drop-in escrito en $DROPIN"

# Los cambios de watchdog requieren re-ejecutar PID 1 (no basta daemon-reload).
systemctl daemon-reexec

# ServiceWatchdogs no es clave de system.conf: se gestiona en runtime.
# Por defecto ya esta activo; se deja explicito por trazabilidad.
systemctl service-watchdogs yes

echo "[*] Configuracion efectiva:"
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec -p ServiceWatchdogs
echo "[*] Estado del hardware:"
grep -H . /sys/class/watchdog/watchdog0/{timeout,timeleft,state,bootstatus}
