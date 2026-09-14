#!/usr/bin/env python3
"""guard_pipeline_benchmark — coste real por ventana de deteccion.

El benchmark anterior media solo la inferencia. El pipeline completo
incluye una etapa de preprocesado que convierte IQ en espectrograma, y
esa etapa tiene coste propio:

    IQ -> STFT -> render 1024x576 -> YOLOv8n -> evento

Este script mide ambas etapas por separado y su suma, que es lo que
determina si el dispositivo alcanza tiempo real.

Parametros de preprocesado tomados de spectrogram_render.py del vertical
de procesado (NFFT=1024, Hann, solape 75 %, 80 dB de rango dinamico,
salida 1024x576 px, colormap parula).

La entrada IQ es sintetica: el coste de la STFT depende del numero de
muestras y de los parametros de ventana, no del contenido de la senal.

Uso:
  guard_pipeline_benchmark.py --fs 40e6 --ventana 0.1
  guard_pipeline_benchmark.py --fs 20e6 --modelo yolov8n_1024x576.onnx
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

try:
    from spectrogram_render import (
        NFFT, OVERLAP, IMG_WIDTH_PX, IMG_HEIGHT_PX,
        iq_to_pdb, render_spectrogram_image,
    )
except ImportError as exc:
    print(f"ERROR: no se pudo importar spectrogram_render: {exc}", file=sys.stderr)
    print("Debe estar en el mismo directorio que este script.", file=sys.stderr)
    sys.exit(1)

RUTA_TEMP = "/sys/class/thermal/thermal_zone0/temp"


def temperatura_c() -> float | None:
    try:
        with open(RUTA_TEMP) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def throttled() -> str | None:
    try:
        s = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return s.split("=", 1)[1] if "=" in s else s
    except (subprocess.SubprocessError, OSError, IndexError):
        return None


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return float(linea.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def iq_sintetico(n: int, rng) -> np.ndarray:
    """IQ complejo aleatorio. El coste de la STFT no depende del contenido."""
    i = rng.standard_normal(n).astype(np.float32)
    q = rng.standard_normal(n).astype(np.float32)
    return (i + 1j * q).astype(np.complex64)


def resumen(lats: list[float]) -> dict:
    lats = sorted(lats)
    return {
        "p50": round(statistics.median(lats), 2),
        "p95": round(lats[max(0, int(len(lats) * 0.95) - 1)], 2),
        "media": round(statistics.fmean(lats), 2),
        "min": round(lats[0], 2),
        "max": round(lats[-1], 2),
    }


def medir_preprocesado(fs: float, ventana_s: float, n_iter: int,
                       calentamiento: int) -> dict:
    n_muestras = int(round(ventana_s * fs))
    rng = np.random.default_rng(12345)
    iq = iq_sintetico(n_muestras, rng)

    n_segmentos = 1 + (n_muestras - NFFT) // (NFFT - OVERLAP)

    print(f"\n{'=' * 62}")
    print("  PREPROCESADO: IQ -> espectrograma")
    print(f"{'=' * 62}")
    print(f"  tasa de muestreo : {fs / 1e6:.1f} MHz")
    print(f"  ventana          : {ventana_s * 1000:.0f} ms")
    print(f"  muestras IQ      : {n_muestras:,} complejas ({iq.nbytes / 1e6:.1f} MB)")
    print(f"  NFFT / solape    : {NFFT} / {OVERLAP} ({100 * OVERLAP // NFFT} %)")
    print(f"  segmentos STFT   : {n_segmentos:,}")
    print(f"  salida           : {IMG_WIDTH_PX}x{IMG_HEIGHT_PX} px")

    for _ in range(calentamiento):
        render_spectrogram_image(iq, fs=fs, fc=2.44e9)

    lat_stft, lat_total = [], []
    rss_max = rss_mb()

    print(f"\n  midiendo ({n_iter} iteraciones)...")
    for k in range(n_iter):
        t0 = time.perf_counter()
        iq_to_pdb(iq, fs=fs)
        t1 = time.perf_counter()
        lat_stft.append((t1 - t0) * 1000.0)

        t2 = time.perf_counter()
        render_spectrogram_image(iq, fs=fs, fc=2.44e9)
        t3 = time.perf_counter()
        lat_total.append((t3 - t2) * 1000.0)

        if k % 5 == 0:
            rss_max = max(rss_max, rss_mb())

    r_stft = resumen(lat_stft)
    r_total = resumen(lat_total)

    print(f"\n  STFT sola        : {r_stft['p50']:8.1f} ms (p50)")
    print(f"  render completo  : {r_total['p50']:8.1f} ms (p50)")
    print(f"  RSS maximo       : {rss_max:8.0f} MB")
    temp = temperatura_c()
    if temp:
        print(f"  temperatura      : {temp:8.1f} C")

    return {
        "tasa_muestreo_hz": fs,
        "ventana_s": ventana_s,
        "muestras_iq": n_muestras,
        "nfft": NFFT,
        "solape": OVERLAP,
        "segmentos_stft": n_segmentos,
        "salida_px": [IMG_WIDTH_PX, IMG_HEIGHT_PX],
        "latencia_stft_ms": r_stft,
        "latencia_render_ms": r_total,
        "rss_max_mb": round(rss_max),
        "temp_c": temp,
    }


def medir_inferencia(ruta_modelo: Path, hilos: int, n_iter: int,
                     calentamiento: int) -> dict | None:
    try:
        import onnxruntime as ort
    except ImportError:
        print("\n  (onnxruntime no disponible: se omite la inferencia)")
        return None

    print(f"\n{'=' * 62}")
    print(f"  INFERENCIA: {ruta_modelo.name}")
    print(f"{'=' * 62}")

    op = ort.SessionOptions()
    op.intra_op_num_threads = hilos
    op.inter_op_num_threads = 1
    op.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    t0 = time.perf_counter()
    sesion = ort.InferenceSession(str(ruta_modelo), sess_options=op,
                                  providers=["CPUExecutionProvider"])
    t_carga = time.perf_counter() - t0

    ent = sesion.get_inputs()[0]
    forma = [d if isinstance(d, int) else 1 for d in ent.shape]
    entrada = np.random.rand(*forma).astype(np.float32)

    print(f"  entrada          : {ent.name} {forma}")
    print(f"  hilos            : {hilos}")
    print(f"  carga modelo     : {t_carga:.2f} s")

    for _ in range(calentamiento):
        sesion.run(None, {ent.name: entrada})

    lats = []
    rss_max = rss_mb()
    print(f"\n  midiendo ({n_iter} iteraciones)...")
    for k in range(n_iter):
        t = time.perf_counter()
        sesion.run(None, {ent.name: entrada})
        lats.append((time.perf_counter() - t) * 1000.0)
        if k % 5 == 0:
            rss_max = max(rss_max, rss_mb())

    r = resumen(lats)
    print(f"\n  latencia p50     : {r['p50']:8.1f} ms")
    print(f"  latencia p95     : {r['p95']:8.1f} ms")
    print(f"  RSS maximo       : {rss_max:8.0f} MB")

    return {
        "modelo": ruta_modelo.name,
        "entrada": forma,
        "hilos": hilos,
        "carga_modelo_s": round(t_carga, 3),
        "latencia_ms": r,
        "rss_max_mb": round(rss_max),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark del pipeline completo")
    p.add_argument("--fs", type=float, default=40e6,
                   help="tasa de muestreo en Hz (def. 40e6, la del USRP B210)")
    p.add_argument("--ventana", type=float, default=0.1,
                   help="duracion de la ventana en segundos (def. 0.1)")
    p.add_argument("--modelo", type=Path, default=None,
                   help="modelo ONNX para medir tambien la inferencia")
    p.add_argument("--iteraciones", type=int, default=20)
    p.add_argument("--calentamiento", type=int, default=3)
    p.add_argument("--hilos", type=int, default=3)
    p.add_argument("--salida", type=Path,
                   default=Path("/opt/guard_device/reports"))
    args = p.parse_args()

    print("=" * 62)
    print("  GUARD — coste del pipeline completo por ventana")
    print("=" * 62)
    thr = throttled()
    print(f"  throttled inicial: {thr}")
    temp = temperatura_c()
    if temp:
        print(f"  temperatura      : {temp:.1f} C")

    pre = medir_preprocesado(args.fs, args.ventana,
                             args.iteraciones, args.calentamiento)

    inf = None
    if args.modelo:
        if not args.modelo.is_file():
            print(f"\nERROR: no existe {args.modelo}", file=sys.stderr)
        else:
            inf = medir_inferencia(args.modelo, args.hilos,
                                   args.iteraciones, args.calentamiento)

    # --- balance
    print(f"\n{'=' * 62}")
    print("  COSTE TOTAL POR VENTANA")
    print(f"{'=' * 62}")

    t_pre = pre["latencia_render_ms"]["p50"]
    print(f"  preprocesado     : {t_pre:8.1f} ms")

    total = t_pre
    if inf:
        t_inf = inf["latencia_ms"]["p50"]
        print(f"  inferencia       : {t_inf:8.1f} ms")
        total += t_inf
    else:
        print("  inferencia       :        — (sin modelo)")

    print(f"  {'-' * 34}")
    print(f"  total            : {total:8.1f} ms")

    presupuesto = args.ventana * 1000.0
    factor = total / presupuesto
    print(f"\n  ventana a cubrir : {presupuesto:8.1f} ms")
    print(f"  factor respecto a tiempo real: {factor:.1f}x")
    if factor > 1:
        print(f"  -> procesa 1 de cada {factor:.1f} ventanas")
        print(f"  -> cobertura temporal del espectro: {100 / factor:.1f} %")
    else:
        print("  -> alcanza tiempo real")

    print(f"\n  throttled final  : {throttled()}")
    t = temperatura_c()
    if t:
        print(f"  temperatura final: {t:.1f} C")

    args.salida.mkdir(parents=True, exist_ok=True)
    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destino = args.salida / f"pipeline-{marca}.json"
    with open(destino, "w") as f:
        json.dump({
            "preprocesado": pre,
            "inferencia": inf,
            "total_ms": round(total, 2),
            "presupuesto_ms": presupuesto,
            "factor_tiempo_real": round(factor, 2),
        }, f, indent=2)
    print(f"\n  resultados en {destino}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
