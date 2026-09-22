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
- Publicacion de las pulsaciones del joystick para el panel.

La histeresis se aplica aqui y no en el firmware: mantiene la politica
en el lado configurable y el microcontrolador simple.

El puente es el unico proceso con el puerto serie abierto, asi que es
tambien el unico que puede oir el joystick. Como el panel es otro
proceso, las pulsaciones se publican en /run/guard/input.json, mismo
patron que el snapshot del detector pero en sentido contrario.

Solo biblioteca estandar mas pyserial.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

PUERTO_DEF = "/dev/ttyAMA3"
BAUD = 115200

INTERVALO_HB = 2.0
INTERVALO_SYS = 10.0
HISTERESIS_DEF = 10.0

HEALTH_FILE = Path("/run/guard/detector.health")
UMBRAL_HEALTH = 30.0          # seccion 3.3
UNIDAD_DETECTOR = "guard-detector-rf.service"

# Pulsaciones del joystick para el panel. Vive en /run, que es tmpfs: no
# desgasta la tarjeta y se limpia sola en cada arranque.
ENTRADA_FILE = Path("/run/guard/input.json")
ACCIONES = ("arriba", "abajo", "ok", "atras")

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


def descomponer(texto: str) -> list:
    """Valida una trama ESP32->Pi y devuelve sus campos.

    Devuelve [] si la trama no es valida. El checksum se comprueba aqui y
    no solo se registra: una trama corrompida por ruido en el cable no
    puede mover el menu del operador. Ya se perdieron dos cables Dupont
    en este montaje, asi que el caso no es hipotetico.
    """
    if not texto.startswith("<") or "*" not in texto:
        return []
    cuerpo, _, cs_recibido = texto[1:].rpartition("*")
    if checksum(cuerpo) != cs_recibido.strip().upper():
        return []
    # El protocolo cierra la lista de campos con un separador final.
    if cuerpo.endswith("|"):
        cuerpo = cuerpo[:-1]
    return cuerpo.split("|")


# --------------------------------------------------------------- pulsaciones

def _seq_inicial() -> int:
    """Continua la numeracion en lugar de volver a empezar.

    Si el puente se reinicia y el contador vuelve a 1, el panel —que
    recuerda el ultimo numero visto— lo tomaria por una pulsacion nueva y
    movria el menu solo. Retomar donde se quedo lo evita.
    """
    try:
        with open(ENTRADA_FILE) as f:
            return int(json.load(f).get("seq", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


_seq = _seq_inicial()


def publicar_pulsacion(accion: str) -> None:
    """Publica una pulsacion para el panel, de forma atomica.

    Fichero temporal y rename, igual que el snapshot del detector: el
    panel puede estar leyendo justo en ese instante, y un JSON a medias
    le haria perder la pulsacion.
    """
    global _seq
    _seq += 1
    datos = {
        "seq": _seq,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
              + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
        "accion": accion,
    }
    try:
        ENTRADA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ENTRADA_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(datos, f, separators=(",", ":"))
        os.replace(tmp, ENTRADA_FILE)
    except OSError as exc:
        print(f"[btn] no se pudo publicar la pulsacion: {exc}",
              file=sys.stderr, flush=True)
        return
    print(f"[btn] {accion} (seq {_seq})", flush=True)


# ------------------------------------------------------------ estado salud

def estado_detector(unidad: str) -> str:
    """OK | DEGRADED | ERROR segun el heartbeat y la unidad systemd."""
    try:
        activo = subprocess.run(
            ["systemctl", "is-active", "--quiet", unidad],
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

def hilo_latido(ser: serial.Serial, inicio: float, unidad: str) -> None:
    while not _parar.is_set():
        uptime = int(time.monotonic() - inicio)
        enviar(ser, f"HB|{uptime}|{estado_detector(unidad)}|")
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

            temp = int(_leer_primer_valor(
                "/sys/class/thermal/thermal_zone0/temp") / 1000)

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
    """Atiende lo que envia el ESP32: pulsaciones, acuses y errores.

    Las pulsaciones se publican para el panel; el resto se registra. Un
    goteo de ERR indica problema fisico en el cable, no en el software.
    """
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
            if not texto:
                continue

            campos = descomponer(texto)
            if campos and campos[0] == "BTN":
                accion = campos[1] if len(campos) > 1 else ""
                if accion in ACCIONES:
                    publicar_pulsacion(accion)
                else:
                    # Una accion desconocida no se propaga: el panel solo
                    # entiende cuatro, y reenviarle cualquier cosa que
                    # llegue por el cable es superficie de fallo gratuita.
                    print(f"[btn] accion no reconocida: {accion!r}",
                          flush=True)
                continue

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
    print(f"pulsaciones -> {ENTRADA_FILE} (desde seq {_seq})", flush=True)

    threading.Thread(target=hilo_latido, args=(ser, inicio, args.unidad),
                     daemon=True).start()
    for destino in (hilo_telemetria, hilo_respuestas):
        threading.Thread(target=destino, args=(ser,), daemon=True).start()

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
