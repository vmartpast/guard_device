#!/usr/bin/env bash
# 01-base.sh — GUARD: provisioning base del SO.
#
# Idempotente: se puede ejecutar varias veces sin efectos acumulativos.
#
# NO libera la UART (ver 03-uart.sh). Mientras no exista un canal de
# administracion alternativo (Ethernet o ESP32), la consola serie en
# ttyS0 es la unica via de acceso si la red falla.

set -euo pipefail

REPO="/opt/guard_device"
BOOT_CFG="/boot/firmware/config.txt"
BOOT_CMD="/boot/firmware/cmdline.txt"
BACKUP_DIR="${REPO}/system/boot/backup-$(date +%Y%m%d-%H%M%S)"
MARKER="# --- GUARD ---"

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }
[[ -f "$BOOT_CFG" ]] || { echo "ERROR: no existe $BOOT_CFG" >&2; exit 1; }

echo "[1/5] Backup de /boot/firmware -> $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -a "$BOOT_CFG" "$BOOT_CMD" "$BACKUP_DIR/"
chown -R guard:guard "${REPO}/system/boot"

echo "[2/5] Journal persistente"
# Raspberry Pi OS fuerza Storage=volatile via
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf.
# Los drop-ins tienen prioridad sobre journald.conf, por lo que hay que
# responder con otro drop-in en /etc (mayor precedencia que /usr/lib).
mkdir -p /var/log/journal /etc/systemd/journald.conf.d
systemd-tmpfiles --create --prefix /var/log/journal
cat > /etc/systemd/journald.conf.d/50-guard-persistent.conf <<'EOF'
# GUARD — journal persistente en disco.
# Necesario para poder auditar el arranque anterior tras un reinicio
# provocado por watchdog o corte de alimentacion (metrica 7.4).
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
EOF
systemctl restart systemd-journald

echo "[3/5] Memoria de GPU al minimo (sin escritorio)"
if grep -q "^gpu_mem=16" "$BOOT_CFG"; then
  echo "      ya configurado"
else
  grep -q "$MARKER" "$BOOT_CFG" || printf '\n%s\n' "$MARKER" >> "$BOOT_CFG"
  sed -i '/^gpu_mem=/d' "$BOOT_CFG"
  echo "gpu_mem=16" >> "$BOOT_CFG"
fi

echo "[4/5] Bluetooth deshabilitado (no usado; libera UART principal)"
systemctl disable --now bluetooth.service 2>/dev/null || true
systemctl disable --now hciuart.service 2>/dev/null || true
grep -q "^dtoverlay=disable-bt" "$BOOT_CFG" || echo "dtoverlay=disable-bt" >> "$BOOT_CFG"

echo "[5/5] Avahi deshabilitado (mDNS innecesario en operacion)"
systemctl disable --now avahi-daemon.service avahi-daemon.socket 2>/dev/null || true

echo
echo "=== HECHO. Requiere reinicio para aplicar cambios de config.txt ==="
echo "Consola serie INTACTA (ttyS0) — via de acceso si falla la red."
