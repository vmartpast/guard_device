#!/usr/bin/env python3
"""guard_panel — panel de estado en terminal.

Presenta en vivo lo mismo que la interfaz fisica del ESP32, pero con el
espacio suficiente para ver el espectro completo y varias emisiones a la
vez. Pensado para desarrollo, depuracion y demostracion: la LCD de 16x2
no da para mas de un renglon de datos.

Reutiliza integramente la logica de analisis de guard_rf.py; este modulo
solo presenta. Si el analisis cambia, el panel lo hereda.

Uso:
  guard_panel.py                     panel en vivo, banda de 2,4 GHz
  guard_panel.py --f-min 5100 --f-max 5900
  guard_panel.py --barridos 6 --duracion 1.0     refresco mas rapido
  guard_panel.py --sin-color                     terminales sin ANSI

Se detiene con Ctrl+C.
"""

import argparse
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from guard_rf import (
        capturar, analizar, identificar, marcar_conocidos,
        cargar_lista_blanca, CONFIG_DEF, SNAPSHOT_FILE,
    )
except ImportError as exc:
    print(f"ERROR: guard_rf.py debe estar en el mismo directorio: {exc}",
          file=sys.stderr)
    sys.exit(1)

# Caracteres de bloque de altura creciente para el perfil de espectro.
BLOQUES = " ▁▂▃▄▅▆▇█"

# Un snapshot mas antiguo que esto significa que el detector no esta
# publicando: se muestra el dato con aviso en lugar de fingir que es actual.
ANTIGUEDAD_MAX_S = 30.0

_running = True


def _stop(signum, frame):
    global _running
    _running = False


class Color:
    """Secuencias ANSI. Se anulan si la salida no es un terminal."""

    def __init__(self, activo: bool = True):
        self.on = activo and sys.stdout.isatty()

    def _c(self, codigo: str, texto: str) -> str:
        return f"\033[{codigo}m{texto}\033[0m" if self.on else texto

    def verde(self, t):    return self._c("32", t)
    def amarillo(self, t): return self._c("33", t)
    def rojo(self, t):     return self._c("31", t)
    def azul(self, t):     return self._c("36", t)
    def gris(self, t):     return self._c("90", t)
    def fuerte(self, t):   return self._c("1", t)

    def limpiar(self) -> str:
        return "\033[2J\033[H" if self.on else ""

    def inicio(self) -> str:
        return "\033[H" if self.on else ""

    def borrar_resto(self) -> str:
        return "\033[J" if self.on else ""

    def cursor(self, visible: bool) -> str:
        if not self.on:
            return ""
        return "\033[?25h" if visible else "\033[?25l"


# ------------------------------------------------------------------ formato

def barra_snr(snr: float, ancho: int = 26, snr_max: float = 30.0) -> str:
    """Barra proporcional al margen sobre el ruido."""
    frac = max(0.0, min(1.0, snr / snr_max))
    llenos = int(frac * ancho)
    return "█" * llenos + "░" * (ancho - llenos)


def perfil_espectro(persistencia: dict, picos: dict, suelo: float,
                    f_min: int, f_max: int, ancho: int) -> str:
    """Perfil del espectro con caracteres de bloque.

    La altura de cada columna es el margen sobre el suelo de ruido, no la
    potencia absoluta: es lo unico que determina la detectabilidad.
    """
    n_bins = f_max - f_min
    if n_bins <= 0 or ancho <= 0:
        return ""

    por_col = max(1, n_bins // ancho)
    columnas = []

    for c in range(min(ancho, (n_bins + por_col - 1) // por_col)):
        lo = f_min + c * por_col
        hi = min(lo + por_col, f_max)
        mejor = 0.0
        for f in range(lo, hi):
            if f in picos:
                mejor = max(mejor, picos[f] - suelo)
        # 24 dB de margen cubre el rango util observado (WiFi cercano
        # ronda los 22 dB sobre el ruido).
        nivel = int(min(1.0, mejor / 24.0) * (len(BLOQUES) - 1))
        columnas.append(BLOQUES[nivel])

    return "".join(columnas)


def desde(segundos: float) -> str:
    if segundos < 0:
        return "--"
    s = int(segundos)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h"


# ------------------------------------------------------------------ fuentes

def leer_snapshot() -> tuple:
    """Lee el analisis publicado por el detector.

    Devuelve (analisis, emisiones, antiguedad_s) o (None, None, None) si no
    hay snapshot legible. El detector escribe de forma atomica, asi que un
    JSON incompleto solo puede deberse a corrupcion real.
    """
    try:
        antiguedad = time.time() - SNAPSHOT_FILE.stat().st_mtime
        with open(SNAPSHOT_FILE) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None, None

    analisis = {
        "n_barridos": d.get("barridos", 0),
        # El detector declara el estado del receptor en el propio
        # snapshot. Sin ese campo el panel no podria separar «todavia no
        # hay medida» de «el receptor no responde», y son dos cosas muy
        # distintas para quien vigila.
        "receptor": d.get("receptor", "ok"),
        "suelo_ruido_db": d.get("suelo_ruido_db", 0.0),
        "umbral_db": d.get("umbral_db", 0.0),
        "persistencia": {int(k): v
                         for k, v in d.get("persistencia_por_bin", {}).items()},
        "picos": {int(k): v for k, v in d.get("picos_por_bin", {}).items()},
    }
    return analisis, d.get("emisiones", []), antiguedad


def capturar_propio(args, lista: list) -> tuple:
    """Barrido autonomo: el panel usa el receptor por su cuenta.

    Solo valido si el detector NO esta corriendo: ambos no pueden tener el
    HackRF abierto a la vez.
    """
    cap = SimpleNamespace(
        f_min=args.f_min, f_max=args.f_max, ancho_bin=args.ancho_bin,
        barridos=args.barridos, duracion=args.duracion,
    )
    barridos = capturar(cap)
    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, lista)
    return analisis, emisiones, 0.0


# ------------------------------------------------------------------- pintado

def pintar(col: Color, args, analisis: dict, emisiones: list,
           lista: list, ultima_deteccion: float, ciclo: int,
           fuente: str = "propia", antiguedad: float = 0.0) -> None:
    ancho_term = shutil.get_terminal_size((100, 30)).columns
    ancho = max(60, min(ancho_term - 2, 110))

    desconocidas = [e for e in emisiones if not e.get("conocido")]
    ahora = datetime.now().strftime("%H:%M:%S")
    obsoleto = (fuente == "detector" and antiguedad > ANTIGUEDAD_MAX_S)

    salida = []
    salida.append(col.inicio())

    # --- cabecera
    titulo = " GUARD — panel de estado "
    relleno = ancho - len(titulo) - len(ahora) - 4
    salida.append(col.fuerte(f"┌─{titulo}" + "─" * max(0, relleno) +
                             f" {ahora} ─┐"))
    salida.append("")

    # --- estado, replicando la maquina de estados del ESP32
    # Un snapshot obsoleto se trata como perdida de enlace: preferible
    # declarar que no hay informacion fiable a mostrar datos viejos como
    # si fueran actuales.
    if obsoleto:
        estado = col.rojo(col.fuerte(
            f"● DETECTOR CAIDO — sin datos desde hace {desde(antiguedad)}"))
    elif analisis.get("receptor") == "sin_datos":
        # El detector esta vivo y publica: lo que falla es el receptor.
        estado = col.rojo(col.fuerte("● RECEPTOR SIN SEÑAL — no se vigila"))
    elif analisis["n_barridos"] == 0:
        estado = col.amarillo("● ESPERANDO AL DETECTOR")
    elif desconocidas:
        estado = col.rojo(col.fuerte("● ALERTA — actividad no identificada"))
    else:
        estado = col.verde("● OPERATIVO — sin actividad no identificada")

    # Dos columnas de posicion fija para que nada se pegue al bajar el
    # ancho del terminal.
    def fila(etiqueta: str, izq: str, der: str = "") -> str:
        return f"  {etiqueta:<14}{izq:<26}{der}"

    ult = (desde(time.monotonic() - ultima_deteccion)
           if ultima_deteccion > 0 else "--")

    origen = ("detector (guard_rf)" if fuente == "detector"
              else "barrido propio")

    salida.append(f"  {'ESTADO':<14}{estado}")
    salida.append(fila("banda", f"{args.f_min}–{args.f_max} MHz",
                       f"suelo   {analisis['suelo_ruido_db']} dBFS"))
    salida.append(fila("origen", origen,
                       f"umbral  {analisis['umbral_db']} dBFS"))
    salida.append(fila("lista blanca", f"{len(lista)} emisor(es)",
                       f"última alerta hace {ult}"))
    salida.append("")

    # --- perfil del espectro
    salida.append(col.gris("  ESPECTRO   (altura = margen sobre el ruido)"))
    etiqueta_ini = f"{args.f_min}"
    etiqueta_fin = f"{args.f_max} MHz"
    ancho_perfil = ancho - len(etiqueta_ini) - len(etiqueta_fin) - 6
    perfil = perfil_espectro(analisis["persistencia"], analisis["picos"],
                             analisis["suelo_ruido_db"],
                             args.f_min, args.f_max, ancho_perfil)
    perfil = perfil.ljust(ancho_perfil)
    salida.append(f"  {etiqueta_ini} {col.azul(perfil)} {etiqueta_fin}")
    salida.append("")

    # --- emisiones
    if not emisiones:
        salida.append(col.gris("  Sin emisiones por encima del umbral."))
        salida.append("")
    else:
        salida.append(col.gris(f"  EMISIONES   ({len(emisiones)}, "
                               f"{len(desconocidas)} sin identificar)"))
        salida.append("")

        for e in emisiones:
            banda = f"{e['f_inicio_mhz']}–{e['f_fin_mhz']} MHz"
            if e["clase"] == "continua":
                clase = "CONTINUA"
                extra = f"{e['tipo']}"
                if e["tipo"] == "wifi":
                    extra = f"WiFi {e['detalle']}"
            else:
                clase = "FHSS    "
                extra = f"{e['canales_visitados']} canales / {e['ancho_mhz']} MHz"

            if e.get("conocido"):
                marca = col.verde("CONOCIDA")
                barra = col.gris(barra_snr(e["snr_db"]))
            else:
                marca = col.rojo(col.fuerte("NO IDENTIFICADA"))
                barra = col.rojo(barra_snr(e["snr_db"]))

            salida.append(f"  {banda:<16}{clase}  {extra}")
            salida.append(f"  {barra}  +{e['snr_db']:.1f} dB   "
                          f"pers. {e['persistencia'] * 100:.0f} %   {marca}")
            if e.get("conocido") and e.get("etiqueta"):
                salida.append(col.gris(f"  {'':<28}{e['etiqueta']}"))
            salida.append("")

    salida.append(col.gris(f"  ciclo {ciclo}   ·   Ctrl+C para salir"))
    salida.append(col.borrar_resto())

    sys.stdout.write("\n".join(salida) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Panel de estado en terminal")
    p.add_argument("--f-min", type=int, default=2400)
    p.add_argument("--f-max", type=int, default=2500)
    p.add_argument("--ancho-bin", type=int, default=1_000_000)
    p.add_argument("--barridos", type=int, default=8,
                   help="barridos por ciclo (def. 8; menos = refresco mas "
                        "rapido pero peor resolucion de persistencia)")
    p.add_argument("--duracion", type=float, default=1.0,
                   help="segundos por barrido individual")
    p.add_argument("--margen", type=float, default=8.0)
    p.add_argument("--config", type=Path, default=CONFIG_DEF)
    p.add_argument("--sin-color", action="store_true")
    p.add_argument("--autonomo", action="store_true",
                   help="barrer por cuenta propia en lugar de consumir el "
                        "analisis del detector (no usar con guard_rf activo)")
    p.add_argument("--sin-receptor", action="store_true",
                   help="no abrir nunca el HackRF: espera al detector. "
                        "Obligatorio al correr como servicio junto a guard_rf")
    args = p.parse_args()

    if args.autonomo and args.sin_receptor:
        print("ERROR: --autonomo y --sin-receptor se excluyen.", file=sys.stderr)
        return 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    col = Color(not args.sin_color)
    lista = cargar_lista_blanca(args.config)

    ultima_deteccion = 0.0
    ciclo = 0
    ts_previo = None

    sys.stdout.write(col.limpiar() + col.cursor(False))
    sys.stdout.flush()

    try:
        while _running:
            ciclo += 1

            # El detector, si esta corriendo, es el unico que habla con el
            # receptor: el panel consume su analisis en lugar de competir
            # por el HackRF. Solo barre por su cuenta si no hay detector.
            analisis = emisiones = None
            fuente, antiguedad = "propia", 0.0

            if not args.autonomo:
                analisis, emisiones, antiguedad = leer_snapshot()
                if analisis is not None:
                    fuente = "detector"

            if analisis is None:
                if args.sin_receptor:
                    # Ejecutandose como servicio junto al detector: no debe
                    # abrir el HackRF bajo ningun concepto, porque se lo
                    # quitaria al detector. Espera a que publique.
                    analisis = {"n_barridos": 0, "suelo_ruido_db": 0.0,
                                "umbral_db": 0.0, "persistencia": {},
                                "picos": {}}
                    emisiones, fuente, antiguedad = [], "detector", 0.0
                else:
                    analisis, emisiones, antiguedad = capturar_propio(args, lista)
                    fuente = "propia"

            if not _running:
                break

            # Solo cuenta como alerta nueva si el analisis ha cambiado; de
            # lo contrario un snapshot estancado reiniciaria el contador en
            # cada refresco.
            ts_actual = (analisis["n_barridos"], analisis["suelo_ruido_db"],
                         len(emisiones))
            if (ts_actual != ts_previo
                    and any(not e.get("conocido") for e in emisiones)):
                ultima_deteccion = time.monotonic()
            ts_previo = ts_actual

            pintar(col, args, analisis, emisiones, lista,
                   ultima_deteccion, ciclo, fuente, antiguedad)

            # Leer un snapshot es barato: se refresca a menudo. Barrer por
            # cuenta propia ya consume su tiempo en la captura.
            if fuente == "detector" and _running:
                time.sleep(1.5)
    finally:
        sys.stdout.write(col.cursor(True))
        sys.stdout.write("\n  panel detenido\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
