#!/usr/bin/env python3
"""guard_benchmark — viabilidad computacional de la inferencia en la Pi 4.

Mide el coste de ejecutar un modelo ONNX en la plataforma GUARD:
latencia por imagen, memoria residente, temperatura y throttling bajo
carga sostenida. NO evalua calidad de deteccion (seccion 6 de
INTEGRACION.md: el pipeline es caja negra).

La entrada es sintetica. Para medir coste computacional basta con que
las dimensiones sean las reales: el tiempo de inferencia de una red
convolucional depende de la forma del tensor, no de su contenido.

Uso:
  guard_benchmark.py modelo.onnx
  guard_benchmark.py modelo.onnx --iteraciones 200 --hilos 3
  guard_benchmark.py *.onnx --sostenido 1800     # 30 min por modelo

Salida: tabla por consola y JSON en reports/ para la memoria.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    print("ERROR: falta onnxruntime. Activar el entorno virtual:", file=sys.stderr)
    print("  source /opt/guard_device/benchmark/.venv/bin/activate", file=sys.stderr)
    sys.exit(1)

# El presupuesto de recursos (seccion 3.4) reserva 3 de 4 nucleos para el
# detector; el cuarto queda para la plataforma. Medir con 4 daria una
# cifra que el dispositivo no puede sostener en operacion real.
HILOS_DEF = 3
ITERACIONES_DEF = 100
CALENTAMIENTO = 10

RUTA_TEMP = "/sys/class/thermal/thermal_zone0/temp"


# --------------------------------------------------------------- sistema

def temperatura_c() -> float | None:
    try:
        with open(RUTA_TEMP) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def throttled() -> str | None:
    """Estado de throttling del firmware. 0x0 = sin incidencias."""
    try:
        salida = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        return salida.split("=", 1)[1] if "=" in salida else salida
    except (subprocess.SubprocessError, OSError, IndexError):
        return None


def rss_mb() -> float:
    """Memoria residente del proceso, en MB."""
    try:
        with open("/proc/self/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return float(linea.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def info_sistema() -> dict:
    modelo_pi = "desconocido"
    try:
        with open("/proc/device-tree/model") as f:
            modelo_pi = f.read().rstrip("\x00").strip()
    except OSError:
        pass

    mem_total = 0.0
    try:
        with open("/proc/meminfo") as f:
            mem_total = float(f.readline().split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        pass

    return {
        "placa": modelo_pi,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "onnxruntime": ort.__version__,
        "nucleos": os.cpu_count(),
        "ram_total_mb": round(mem_total),
    }


# ------------------------------------------------------------- benchmark

def forma_entrada(sesion) -> tuple[str, list]:
    ent = sesion.get_inputs()[0]
    forma = [d if isinstance(d, int) else 1 for d in ent.shape]
    return ent.name, forma


def medir(ruta_modelo: Path, iteraciones: int, hilos: int,
          sostenido: float) -> dict:
    print(f"\n{'=' * 62}")
    print(f"  {ruta_modelo.name}")
    print(f"{'=' * 62}")

    opciones = ort.SessionOptions()
    opciones.intra_op_num_threads = hilos
    opciones.inter_op_num_threads = 1
    opciones.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    rss_previo = rss_mb()
    t0 = time.perf_counter()
    sesion = ort.InferenceSession(
        str(ruta_modelo), sess_options=opciones,
        providers=["CPUExecutionProvider"],
    )
    t_carga = time.perf_counter() - t0
    rss_carga = rss_mb()

    nombre, forma = forma_entrada(sesion)
    entrada = np.random.rand(*forma).astype(np.float32)

    print(f"  entrada        : {nombre} {forma}")
    print(f"  hilos          : {hilos}")
    print(f"  carga modelo   : {t_carga:.2f} s")
    print(f"  RSS tras carga : {rss_carga:.0f} MB (+{rss_carga - rss_previo:.0f})")

    temp_inicial = temperatura_c()
    print(f"  temp. inicial  : {temp_inicial:.1f} C" if temp_inicial else "")

    print(f"\n  calentamiento ({CALENTAMIENTO} iteraciones)...")
    for _ in range(CALENTAMIENTO):
        sesion.run(None, {nombre: entrada})

    # --- medicion principal
    print(f"  midiendo ({iteraciones} iteraciones)...")
    latencias = []
    rss_max = rss_carga
    for i in range(iteraciones):
        t = time.perf_counter()
        sesion.run(None, {nombre: entrada})
        latencias.append((time.perf_counter() - t) * 1000.0)
        if i % 10 == 0:
            rss_max = max(rss_max, rss_mb())

    latencias.sort()
    p50 = statistics.median(latencias)
    p95 = latencias[int(len(latencias) * 0.95) - 1]
    media = statistics.fmean(latencias)

    temp_tras = temperatura_c()
    thr_tras = throttled()

    resultado = {
        "modelo": ruta_modelo.name,
        "entrada": forma,
        "hilos": hilos,
        "iteraciones": iteraciones,
        "carga_modelo_s": round(t_carga, 3),
        "rss_carga_mb": round(rss_carga),
        "rss_max_mb": round(rss_max),
        "latencia_ms": {
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "media": round(media, 2),
            "min": round(latencias[0], 2),
            "max": round(latencias[-1], 2),
        },
        "fps_equivalente": round(1000.0 / p50, 2),
        "temp_inicial_c": temp_inicial,
        "temp_final_c": temp_tras,
        "throttled": thr_tras,
    }

    print(f"\n  latencia p50   : {p50:.1f} ms   ({1000 / p50:.1f} img/s)")
    print(f"  latencia p95   : {p95:.1f} ms")
    print(f"  RSS maximo     : {rss_max:.0f} MB")
    if temp_tras:
        print(f"  temperatura    : {temp_tras:.1f} C")
    if thr_tras:
        estado = "sin incidencias" if thr_tras == "0x0" else "REVISAR"
        print(f"  throttled      : {thr_tras}  ({estado})")

    # --- carga sostenida, opcional
    if sostenido > 0:
        print(f"\n  carga sostenida durante {sostenido / 60:.0f} min...")
        resultado["sostenido"] = ejecucion_sostenida(sesion, nombre, entrada,
                                                    sostenido)

    return resultado


def ejecucion_sostenida(sesion, nombre, entrada, segundos: float) -> dict:
    """Ejecucion prolongada: detecta degradacion termica y throttling.

    Una latencia buena en 100 iteraciones no garantiza que el dispositivo
    la sostenga: la Pi 4 sin disipacion adecuada reduce frecuencia al
    alcanzar ~80 C, y el efecto solo aparece tras varios minutos.
    """
    inicio = time.monotonic()
    muestras = []
    n = 0
    proxima_muestra = 0.0

    while time.monotonic() - inicio < segundos:
        t = time.perf_counter()
        sesion.run(None, {nombre: entrada})
        lat = (time.perf_counter() - t) * 1000.0
        n += 1

        transcurrido = time.monotonic() - inicio
        if transcurrido >= proxima_muestra:
            temp = temperatura_c()
            muestras.append({
                "t_s": round(transcurrido),
                "latencia_ms": round(lat, 2),
                "temp_c": temp,
                "rss_mb": round(rss_mb()),
                "throttled": throttled(),
            })
            print(f"    {transcurrido / 60:5.1f} min  "
                  f"{lat:6.1f} ms  "
                  f"{temp:.1f} C" if temp else "")
            proxima_muestra = transcurrido + 60

    primeras = [m["latencia_ms"] for m in muestras[:3]]
    ultimas = [m["latencia_ms"] for m in muestras[-3:]]
    degradacion = 0.0
    if primeras and ultimas:
        degradacion = 100.0 * (statistics.fmean(ultimas) /
                               statistics.fmean(primeras) - 1)

    temps = [m["temp_c"] for m in muestras if m["temp_c"]]
    return {
        "duracion_s": round(time.monotonic() - inicio),
        "inferencias": n,
        "temp_max_c": max(temps) if temps else None,
        "degradacion_latencia_pct": round(degradacion, 1),
        "throttled_final": throttled(),
        "muestras": muestras,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark de inferencia GUARD")
    p.add_argument("modelos", nargs="+", type=Path, help="ficheros .onnx")
    p.add_argument("--iteraciones", type=int, default=ITERACIONES_DEF)
    p.add_argument("--hilos", type=int, default=HILOS_DEF,
                   help=f"nucleos para inferencia (def. {HILOS_DEF}, seccion 3.4)")
    p.add_argument("--sostenido", type=float, default=0,
                   help="segundos de carga sostenida por modelo (0 = omitir)")
    p.add_argument("--salida", type=Path,
                   default=Path("/opt/guard_device/reports"))
    args = p.parse_args()

    faltan = [m for m in args.modelos if not m.is_file()]
    if faltan:
        for m in faltan:
            print(f"ERROR: no existe {m}", file=sys.stderr)
        return 1

    sistema = info_sistema()
    print("=" * 62)
    print("  GUARD — benchmark de viabilidad computacional")
    print("=" * 62)
    for k, v in sistema.items():
        print(f"  {k:14}: {v}")

    thr = throttled()
    if thr and thr != "0x0":
        print(f"\n  AVISO: throttled={thr} antes de empezar.")
        print("  Las medidas pueden no ser representativas.")

    resultados = [medir(m, args.iteraciones, args.hilos, args.sostenido)
                  for m in args.modelos]

    # --- resumen
    print(f"\n{'=' * 62}")
    print("  RESUMEN")
    print(f"{'=' * 62}")
    print(f"  {'modelo':<22} {'p50 ms':>8} {'p95 ms':>8} {'RSS MB':>8} {'img/s':>7}")
    print(f"  {'-' * 22} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 7}")
    for r in resultados:
        print(f"  {r['modelo']:<22} "
              f"{r['latencia_ms']['p50']:>8.1f} "
              f"{r['latencia_ms']['p95']:>8.1f} "
              f"{r['rss_max_mb']:>8} "
              f"{r['fps_equivalente']:>7.1f}")

    args.salida.mkdir(parents=True, exist_ok=True)
    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destino = args.salida / f"benchmark-{marca}.json"
    with open(destino, "w") as f:
        json.dump({"sistema": sistema, "resultados": resultados}, f, indent=2)
    print(f"\n  resultados en {destino}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
