#!/usr/bin/env bash
# 03-boot-trim.sh — GUARD: reduccion del tiempo de arranque.
#
# Idempotente. Elimina de la cadena de arranque servicios sin funcion en
# un dispositivo embebido que opera sin red y se aprovisiona mediante los
# scripts de este repositorio.
#
# NO toca la red ni la consola serie: ambas son vias de administracion
# necesarias mientras no exista canal alternativo (Ethernet o ESP32).

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "ERROR: ejecutar con sudo" >&2; exit 1; }

echo "[1/2] cloud-init deshabilitado (aprovisionamiento propio via repo)"
# El metodo soportado es el fichero centinela; enmascarar las unidades
# sueltas deja generadores activos que siguen costando tiempo.
touch /etc/cloud/cloud-init.disabled
for u in cloud-init-main cloud-init-local cloud-init-network cloud-config cloud-final; do
  systemctl disable "${u}.service" 2>/dev/null || true
done

echo "[2/2] Estado"
ls -l /etc/cloud/cloud-init.disabled
systemctl is-enabled cloud-init-main.service 2>&1 || true

echo
echo "=== HECHO. Reiniciar y comparar con systemd-analyze ==="
