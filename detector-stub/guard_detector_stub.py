#!/usr/bin/env python3
"""
guard_detector_stub — implementacion minima de la especificacion de
plataforma (INTEGRACION.md seccion 3).

Emite eventos JSON Lines por stdout y mantiene el fichero heartbeat que
la plataforma vigila. No realiza deteccion: sustituye al detector real
para desarrollar y validar la vertical de plataforma.

Modos (seccion 4):
  idle      solo status + heartbeat
  sporadic  deteccion confirmada cada 30-120 s
  burst     rafaga de detecciones en pocos segundos
  flaky     deja de emitir heartbeat / sale con error
  load      consume RAM y CPU segun presupuesto
"""

import argparse
import json
import os
import random
import signal
import socket
import threading
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEALTH_FILE = Path("/run/guard/detector.health")
HEARTBEAT_INTERVAL = 5.0        # seccion 3.3 exige <= 10 s
STATUS_INTERVAL = 30.0

MODELS = ["dji_mini3", "dji_mavic3", "autel_evo2", None]

_running = True
_stop_event = None  # threading.Event, inicializado en main()


def _stop(signum, frame):
    global _running
    _running = False
    if _stop_event is not None:
        _stop_event.set()


def now_iso() -> str:
    """Marca de tiempo UTC con milisegundos, formato de la seccion 3.2."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    """Un objeto JSON por linea, vaciado inmediato (journald lo captura)."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_status(mode: str, extra: dict | None = None) -> None:
    ev = {"ts": now_iso(), "type": "status", "mode": mode}
    if extra:
        ev.update(extra)
    emit(ev)


def emit_detection(confirmed: bool = True) -> None:
    emit({
        "ts": now_iso(),
        "type": "detection",
        "confirmed": confirmed,
        "label": "drone",
        "model": random.choice(MODELS),
        "confidence": round(random.uniform(0.72, 0.98), 2),
        "window_s": 0.1,
        "bursts": random.randint(8, 22),
        "rssi_dbfs": round(random.uniform(-78.0, -31.0), 1),
    })


def sd_notify(state: str) -> None:
    """Notificacion a systemd (Type=notify / WatchdogSec=).

    Implementado sobre el socket de NOTIFY_SOCKET para no depender de
    python3-systemd. Sin systemd, es una operacion nula.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):          # socket abstracto
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


def touch_health() -> None:
    """Heartbeat de la seccion 3.3. La plataforma lo considera caido a los 30 s."""
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.touch()
    except OSError as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo actualizar heartbeat: {exc}"})


def run_idle(args) -> int:
    """Solo status y heartbeat: valida arranque, health check y UI en reposo."""
    started = time.monotonic()
    emit_status("idle", {"pid": os.getpid()})
    sd_notify("READY=1")
    last_status = started

    while _running:
        touch_health()
        sd_notify("WATCHDOG=1")
        if time.monotonic() - last_status >= STATUS_INTERVAL:
            emit_status("idle", {"uptime_s": round(time.monotonic() - started)})
            last_status = time.monotonic()
        # Espera interrumpible: SIGTERM corta de inmediato, no tras el sleep.
        _stop_event.wait(HEARTBEAT_INTERVAL)

    sd_notify("STOPPING=1")
    emit_status("idle", {"stopping": True})
    return 0


MODES = {
    "idle": run_idle,
}


def main() -> int:
    p = argparse.ArgumentParser(description="Stub del detector GUARD")
    p.add_argument("--mode", choices=sorted(MODES), default="idle",
                   help="modo de operacion (seccion 4)")
    args = p.parse_args()

    global _stop_event
    _stop_event = threading.Event()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    return MODES[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
