#!/usr/bin/env python3
"""guard_persistencia — clasificación de emisiones por persistencia temporal.

Un barrido acumulado no distingue una emisión continua de una de salto de
frecuencia: ambas aparecen como energía en la banda. Con suficiente tiempo
de acumulación, una emisión FHSS llega a ocupar el espectro completo y
destruye la estimación del suelo de ruido, cegando al detector.

Este módulo resuelve esa limitación tomando N barridos cortos y midiendo,
para cada bin de frecuencia, en qué fracción de ellos aparece ocupado.

    persistencia alta  ->  emisión continua (WiFi, vídeo analógico, enlace fijo)
    persistencia baja  ->  ocupación esporádica (FHSS, ráfagas, interferencia)

El criterio no es la frecuencia ni la potencia, sino el comportamiento en
el tiempo. Es lo que separa físicamente ambas familias de emisión.

Uso:
  guard_persistencia.py
  guard_persistencia.py --f-min 2400 --f-max 2500 --barridos 12
  guard_persistencia.py --json          salida estructurada
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

N_BARRIDOS = 10
DURACION_BARRIDO_S = 1.5
ANCHO_BIN_HZ = 1_000_000

# Margen sobre el suelo de ruido para considerar un bin ocupado en un
# barrido individual. Más bajo que en el detector de energía: aquí no se
# busca separar una emisión fuerte, sino registrar presencia.
MARGEN_DB = 8.0

# Un bin presente en más del 70 % de los barridos se considera emisión
# continua. Por debajo del 25 %, ocupación esporádica.
# Medido experimentalmente: un canal WiFi con trafico real presenta
# persistencia irregular entre 40 % y 80 % segun la carga del momento, no
# cercana al 100 %. Un umbral alto lo fragmenta en bins sueltos. La
# separacion real frente a FHSS esta en torno al 30 %.
UMBRAL_CONTINUA = 0.35
UMBRAL_ESPORADICA = 0.25

# Una emisión de salto ocupa muchos bins distintos con baja persistencia
# cada uno. Por debajo de este número de bins, es más probable que se
# trate de ruido impulsivo que de un sistema FHSS.
MIN_BINS_FHSS = 6


def mediana(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def un_barrido(f_min: int, f_max: int, ancho_bin: int,
               duracion: float) -> dict[int, float]:
    """Un barrido corto. Devuelve {frecuencia_mhz: potencia_db}."""
    try:
        proc = subprocess.Popen(
            ["hackrf_sweep", "-f", f"{f_min}:{f_max}", "-w", str(ancho_bin)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    potencias: dict[int, float] = {}
    t0 = time.monotonic()
    try:
        for linea in proc.stdout:
            if time.monotonic() - t0 > duracion:
                break
            campos = linea.strip().split(", ")
            if len(campos) < 7:
                continue
            try:
                f_ini = int(campos[2])
                paso = float(campos[4])
                for k, valor in enumerate(campos[6:]):
                    f = int((f_ini + k * paso) / 1e6)
                    db = float(valor)
                    if f not in potencias or db > potencias[f]:
                        potencias[f] = db
            except (ValueError, IndexError):
                continue
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    return potencias


def analizar_persistencia(barridos: list[dict[int, float]],
                          margen: float) -> dict:
    """Calcula, por bin, en qué fracción de barridos aparece ocupado."""
    n = len(barridos)
    ocupaciones: dict[int, int] = {}
    picos: dict[int, float] = {}
    suelos = []

    for potencias in barridos:
        if not potencias:
            continue
        suelo = mediana(list(potencias.values()))
        suelos.append(suelo)
        umbral = suelo + margen
        for f, db in potencias.items():
            if db >= umbral:
                ocupaciones[f] = ocupaciones.get(f, 0) + 1
                if f not in picos or db > picos[f]:
                    picos[f] = db

    persistencia = {f: c / n for f, c in ocupaciones.items()}

    continuas = sorted(f for f, p in persistencia.items() if p >= UMBRAL_CONTINUA)
    esporadicas = sorted(f for f, p in persistencia.items()
                         if p <= UMBRAL_ESPORADICA)
    intermedias = sorted(f for f, p in persistencia.items()
                         if UMBRAL_ESPORADICA < p < UMBRAL_CONTINUA)

    return {
        "barridos": n,
        "suelo_ruido_medio_db": round(mediana(suelos), 1) if suelos else None,
        "persistencia": persistencia,
        "picos": picos,
        "continuas": continuas,
        "esporadicas": esporadicas,
        "intermedias": intermedias,
    }


def agrupar_contiguas(frecuencias: list[int]) -> list[tuple[int, int]]:
    """Agrupa frecuencias contiguas en rangos (inicio, fin)."""
    if not frecuencias:
        return []
    grupos = []
    ini = ant = frecuencias[0]
    for f in frecuencias[1:]:
        if f == ant + 1:
            ant = f
        else:
            grupos.append((ini, ant))
            ini = ant = f
    grupos.append((ini, ant))
    return grupos


def interpretar(analisis: dict) -> list[dict]:
    """Traduce el análisis de persistencia a emisiones identificadas."""
    emisiones = []
    persistencia = analisis["persistencia"]
    picos = analisis["picos"]

    # Agrupar por contiguidad antes de clasificar: un bloque de bins
    # adyacentes con persistencia media es una unica emision en frecuencia
    # fija, no varias emisiones de 1 MHz.
    fijas = sorted(set(analisis["continuas"]) | set(analisis["intermedias"]))
    for ini, fin in agrupar_contiguas(fijas):
        if fin - ini + 1 < 3:
            continue
        bins = list(range(ini, fin + 1))
        emisiones.append({
            "tipo": "continua",
            "f_inicio_mhz": ini,
            "f_fin_mhz": fin,
            "ancho_mhz": len(bins),
            "persistencia_media": round(
                sum(persistencia[f] for f in bins) / len(bins), 2),
            "pico_db": round(max(picos[f] for f in bins), 1),
            "interpretacion": "emisión permanente en frecuencia fija",
        })

    ya_asignados = {f for e in emisiones
                    for f in range(e["f_inicio_mhz"], e["f_fin_mhz"] + 1)}
    esporadicas = [f for f in analisis["esporadicas"] if f not in ya_asignados]
    if len(esporadicas) >= MIN_BINS_FHSS:
        dispersion = max(esporadicas) - min(esporadicas)
        emisiones.append({
            "tipo": "salto_frecuencia",
            "f_inicio_mhz": min(esporadicas),
            "f_fin_mhz": max(esporadicas),
            "bins_visitados": len(esporadicas),
            "dispersion_mhz": dispersion,
            "persistencia_media": round(
                sum(persistencia[f] for f in esporadicas) / len(esporadicas), 2),
            "pico_db": round(max(picos[f] for f in esporadicas), 1),
            "interpretacion": (f"ocupación esporádica repartida sobre "
                               f"{dispersion} MHz — compatible con FHSS"),
        })

    return emisiones


def imprimir_mapa(analisis: dict, f_min: int, f_max: int) -> None:
    """Mapa de persistencia por frecuencia, en texto."""
    persistencia = analisis["persistencia"]
    print(f"\n  Mapa de persistencia ({analisis['barridos']} barridos)")
    print(f"  suelo de ruido medio: {analisis['suelo_ruido_medio_db']} dB\n")
    print("       0%                                              100%")
    print("       |------------------------------------------------|")

    for f in range(f_min, f_max):
        p = persistencia.get(f, 0.0)
        if p == 0:
            continue
        barra = int(p * 48)
        if p >= UMBRAL_CONTINUA:
            marca = "="
        elif p <= UMBRAL_ESPORADICA:
            marca = "."
        else:
            marca = "-"
        print(f"  {f}  {marca * barra}{' ' * (48 - barra)}| {p * 100:3.0f}%")

    print("\n  leyenda:  = continua    - intermedia    . esporádica")


def imprimir_emisiones(emisiones: list[dict]) -> None:
    if not emisiones:
        print("\n  Sin emisiones identificadas.")
        return

    print(f"\n  {len(emisiones)} emisión(es) identificada(s):\n")
    for e in emisiones:
        if e["tipo"] == "continua":
            print(f"  CONTINUA   {e['f_inicio_mhz']}-{e['f_fin_mhz']} MHz "
                  f"({e['ancho_mhz']} MHz)")
        else:
            print(f"  FHSS       {e['f_inicio_mhz']}-{e['f_fin_mhz']} MHz "
                  f"({e['bins_visitados']} canales visitados)")
        print(f"             persistencia {e['persistencia_media'] * 100:.0f} %, "
              f"pico {e['pico_db']} dB")
        print(f"             {e['interpretacion']}\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Clasificación de emisiones por persistencia temporal")
    p.add_argument("--f-min", type=int, default=2400)
    p.add_argument("--f-max", type=int, default=2500)
    p.add_argument("--barridos", type=int, default=N_BARRIDOS)
    p.add_argument("--duracion", type=float, default=DURACION_BARRIDO_S,
                   help="segundos por barrido individual")
    p.add_argument("--ancho-bin", type=int, default=ANCHO_BIN_HZ)
    p.add_argument("--margen", type=float, default=MARGEN_DB)
    p.add_argument("--json", action="store_true", help="salida JSON")
    p.add_argument("--guardar", type=Path, default=None)
    args = p.parse_args()

    if not args.json:
        print("=" * 70)
        print("  GUARD — análisis de persistencia temporal")
        print("=" * 70)
        print(f"\n  banda      : {args.f_min}-{args.f_max} MHz")
        print(f"  barridos   : {args.barridos} x {args.duracion:.1f} s")
        print(f"  duración   : ~{args.barridos * args.duracion:.0f} s\n")

    barridos = []
    for i in range(args.barridos):
        if not args.json:
            print(f"\r  barrido {i + 1}/{args.barridos}...", end="", flush=True)
        barridos.append(un_barrido(args.f_min, args.f_max,
                                   args.ancho_bin, args.duracion))

    if not args.json:
        print("\r" + " " * 40 + "\r", end="")

    if not any(barridos):
        print("ERROR: ningún barrido devolvió datos.", file=sys.stderr)
        return 1

    analisis = analizar_persistencia(barridos, args.margen)
    emisiones = interpretar(analisis)

    salida = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "banda_mhz": [args.f_min, args.f_max],
        "barridos": args.barridos,
        "duracion_barrido_s": args.duracion,
        "suelo_ruido_db": analisis["suelo_ruido_medio_db"],
        "emisiones": emisiones,
    }

    if args.json:
        print(json.dumps(salida, indent=2, ensure_ascii=False))
    else:
        imprimir_mapa(analisis, args.f_min, args.f_max)
        imprimir_emisiones(emisiones)

    if args.guardar:
        args.guardar.parent.mkdir(parents=True, exist_ok=True)
        salida["persistencia_por_bin"] = {
            str(f): round(p, 3) for f, p in sorted(analisis["persistencia"].items())
        }
        with open(args.guardar, "w") as f:
            json.dump(salida, f, indent=2, ensure_ascii=False)
        if not args.json:
            print(f"  resultado en {args.guardar}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
