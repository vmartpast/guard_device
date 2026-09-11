#!/usr/bin/env python3
"""guard_bridge — puente entre el detector y la interfaz fisica.

Lee los eventos JSON Lines que emite el detector (o el stub) por el
journal de systemd y los traduce a tramas UART para el ESP32, segun
docs/PROTOCOLO_UART.md.

Responsabilidades:

- Latido cada 2 s con el estado de salud del detector (seccion 3.3).
- Reenvio de detecciones confirmadas, aplicando la histeresis de la
  seccion 3.5: las detecciones de un mismo episodio no re-alertan.
- Telemetria del sistema cada 10 s.
- Registro de las respuestas del ESP32 (ACK, BTN, ERR).

La histeresis se aplica aqui y no en el firmware: mantiene la politica
en el lado configurable y el microcontrolador simple.

Solo biblioteca estandar mas pyserial.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial

PUERTO_DEF = "/dev/ttyAMA3"
BAUD = 115200

INTERVALO_HB = 2.0
INTERVALO_SYS = 10.0
HISTERESIS_DEF = 10.0

HEALTH_FILE = Path("/run/guard/detector.health")
UMBRAL_HEALTH = 30.0          # seccion 3.3
UNIDAD_DETECTOR = "guard-detector-stub.service"

_parar = threading.Event()
_lock_serie = threading.Lock()


# ------------------------------------------------------------------ tramas

def checksum(cuerpo: str) -> str:
    cs = 0
    for b in cuerpo.encode():
        cs ^= b
    return f"{cs:02X}"


def enviar(ser: serial.Serial, cuerpo: str) -> None:
    """Emite una trama Pi->ESP32 con prefijo, checksum y terminador."""
    trama = f">{cuerpo}*{checksum(cuerpo)}\n"
    with _lock_serie:
        ser.write(trama.encode())


# ------------------------------------------------------------ estado salud

def estado_detector() -> str:
    """OK | DEGRADED | ERROR segun el heartbeat y la unidad systemd."""
    try:
        activo = subprocess.run(
            ["systemctl", "is-active", "--quiet", UNIDAD_DETECTOR],
            timeout=2,
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        activo = False

    if not activo:
        return "ERROR"

    try:
        edad = time.time() - HEALTH_FILE.stat().st_mtime
    except OSError:
        return "DEGRADED"

    return "OK" if edad <= UMBRAL_HEALTH else "DEGRADED"


# ------------------------------------------------------------------ hilos

def hilo_latido(ser: serial.Serial, inicio: float) -> None:
    while not _parar.is_set():
        uptime = int(time.monotonic() - inicio)
        enviar(ser, f"HB|{uptime}|{estado_detector()}|")
        _parar.wait(INTERVALO_HB)


def _leer_primer_valor(ruta: str, idx: int = 0) -> float:
    with open(ruta) as f:
        return float(f.read().split()[idx])


def hilo_telemetria(ser: serial.Serial) -> None:
    prev = None
    while not _parar.is_set():
        try:
            with open("/proc/stat") as f:
                campos = [float(x) for x in f.readline().split()[1:]]
            total, ocioso = sum(campos), campos[3]
            cpu = 0
            if prev:
                dt, di = total - prev[0], ocioso - prev[1]
                if dt > 0:
                    cpu = int(100 * (dt - di) / dt)
            prev = (total, ocioso)

            temp = int(_leer_primer_valor("/sys/class/thermal/thermal_zone0/temp") / 1000)

            with open("/proc/meminfo") as f:
                mem = {}
                for linea in f:
                    k, v = linea.split(":", 1)
                    mem[k] = float(v.split()[0])
            usada = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])

            enviar(ser, f"SYS|{cpu}|{temp}|{int(usada)}|")
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(f"[telemetria] omitida: {exc}", file=sys.stderr)

        _parar.wait(INTERVALO_SYS)


def hilo_respuestas(ser: serial.Serial) -> None:
    """Registra lo que envia el ESP32. ERR frecuentes indican problema fisico."""
    buf = b""
    while not _parar.is_set():
        try:
            datos = ser.read(64)
        except serial.SerialException:
            break
        if not datos:
            continue
        buf += datos
        while b"\n" in buf:
            linea, buf = buf.split(b"\n", 1)
            texto = linea.decode(errors="replace").strip()
            if texto:
                print(f"[esp32] {texto}", flush=True)


# ------------------------------------------------------------------ eventos

def seguir_eventos(unidad: str):
    """Itera los eventos JSON del detector leidos del journal."""
    proc = subprocess.Popen(
        ["journalctl", "-u", unidad, "-f", "-n", "0", "-o", "cat"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        for linea in proc.stdout:
            linea = linea.strip()
            if not linea.startswith("{"):
                continue
            try:
                yield json.loads(linea)
            except json.JSONDecodeError:
                continue
    finally:
        proc.terminate()


def main() -> int:
    p = argparse.ArgumentParser(description="Puente detector -> interfaz fisica")
    p.add_argument("--puerto", default=PUERTO_DEF)
    p.add_argument("--unidad", default=UNIDAD_DETECTOR)
    p.add_argument("--histeresis", type=float, default=HISTERESIS_DEF,
                   help="segundos sin re-alertar dentro del mismo episodio")
    args = p.parse_args()

    try:
        ser = serial.Serial(args.puerto, BAUD, timeout=0.5)
    except serial.SerialException as exc:
        print(f"ERROR: no se pudo abrir {args.puerto}: {exc}", file=sys.stderr)
        return 1

    inicio = time.monotonic()
    print(f"puente iniciado: {args.puerto} @ {BAUD}, unidad {args.unidad}",
          flush=True)

    for destino in (hilo_latido, hilo_telemetria, hilo_respuestas):
        argumentos = (ser, inicio) if destino is hilo_latido else (ser,)
        threading.Thread(target=destino, args=argumentos, daemon=True).start()

    ultima_alerta = 0.0
    suprimidas = 0

    try:
        for ev in seguir_eventos(args.unidad):
            if ev.get("type") != "detection" or not ev.get("confirmed"):
                continue

            ahora = time.monotonic()
            if ahora - ultima_alerta < args.histeresis:
                suprimidas += 1
                continue

            if suprimidas:
                print(f"[histeresis] {suprimidas} detecciones del episodio "
                      f"anterior no re-alertaron", flush=True)
                suprimidas = 0

            rssi = ev.get("rssi_dbfs")
            modelo = ev.get("model") or "-"
            conf = ev.get("confidence", 0.0)

            campo_rssi = "-" if rssi is None else f"{rssi:.1f}"
            enviar(ser, f"DET|{campo_rssi}|{modelo}|{conf:.2f}|")
            ultima_alerta = ahora
            print(f"[alerta] {modelo} {campo_rssi} conf={conf:.2f}", flush=True)

    except KeyboardInterrupt:
        pass
    finally:
        _parar.set()
        time.sleep(0.3)
        ser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
