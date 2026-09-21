#!/usr/bin/env python3
"""guard_panel — panel de estado en terminal.

Presenta en vivo lo mismo que la interfaz fisica del ESP32, pero con el
espacio suficiente para ver el espectro completo, su evolucion reciente y
varias emisiones a la vez. Pensado para desarrollo, depuracion y
demostracion: la LCD de 16x2 no da para mas de un renglon de datos.

Reutiliza integramente la logica de analisis de guard_rf.py; este modulo
solo presenta. Si el analisis cambia, el panel lo hereda.

Que muestra, y por que
----------------------
Un panel de vigilancia que solo muestra el instante obliga a quien mira a
estar mirando. Por eso hay tres capas de informacion con horizontes
distintos:

    ahora       estado, emisiones y perfil del espectro
    reciente    cascada: las ultimas N medidas, una fila por ciclo
    historico   registro de emisiones nuevas, con la hora

La cascada es lo que convierte el panel en un instrumento: una emision
intermitente no se distingue de un pico de ruido en una sola foto, pero en
quince filas apiladas se ve sola.

La franja de salud de la plataforma esta por la misma razon por la que
existe el estado 'receptor sin senal': un equipo desatendido que se
degrada en silencio es peor que uno que falla ruidosamente. La
temperatura y el estrangulamiento termico importan de forma medible en
esta plataforma —el benchmark registro degradacion de latencia a partir
de 82 grados— asi que estan a la vista.

Uso:
  guard_panel.py                     panel en vivo, banda de 2,4 GHz
  guard_panel.py --f-min 5100 --f-max 5900
  guard_panel.py --barridos 6 --duracion 1.0     refresco mas rapido
  guard_panel.py --sin-color                     terminales sin ANSI
  guard_panel.py --sin-cascada                   pantallas muy bajas

Se detiene con Ctrl+C.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
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

# Escala de la cascada. Los cortes estan en dB sobre el suelo de ruido, no
# en potencia absoluta: es lo unico que determina la detectabilidad, y hace
# que la cascada siga siendo legible cuando el suelo cambia.
CASCADA_NIVELES = [
    (20.0, "█", "rojo"),
    (14.0, "▓", "amarillo"),
    (8.0,  "▒", "azul"),
    # Por debajo de 4 dB no hay nada que mirar: son las fluctuaciones
    # propias del suelo de ruido, y pintarlas llena la cascada de mota
    # que esconde lo que si importa.
    (4.0,  "░", "gris"),
    (0.0,  " ", "gris"),
]

# Profundidad maxima de la cascada. Mas filas no aportan: a 1,5 s por
# ciclo, veinte filas ya son medio minuto de historia.
CASCADA_MAX = 20

# Entradas del registro de emisiones nuevas.
REGISTRO_MAX = 40

# Un snapshot mas antiguo que esto significa que el detector no esta
# publicando: se muestra el dato con aviso en lugar de fingir que es actual.
ANTIGUEDAD_MAX_S = 30.0

# La salud de la plataforma cambia despacio y su lectura cuesta un proceso
# externo. Se refresca cada pocos segundos, no en cada pintado.
SALUD_TTL_S = 10.0

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

    def por_nombre(self, nombre: str, t: str) -> str:
        return getattr(self, nombre)(t)

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


def margenes_por_columna(picos: dict, suelo: float, f_min: int, f_max: int,
                         ancho: int) -> list:
    """Reduce el espectro a `ancho` columnas de margen sobre el ruido.

    Es la base tanto del perfil instantaneo como de la cascada, y por eso
    vive aparte: las dos vistas tienen que compartir exactamente el mismo
    binado, o una emision aparecera desplazada entre ellas.

    Se toma el maximo de cada grupo de bins, no la media: una emision
    estrecha dentro de un grupo ancho se diluiria hasta desaparecer, y en
    deteccion importa mas no perderla que representar su forma.
    """
    n_bins = f_max - f_min
    if n_bins <= 0 or ancho <= 0:
        return []

    por_col = max(1, n_bins // ancho)
    n_col = min(ancho, (n_bins + por_col - 1) // por_col)
    columnas = []

    for c in range(n_col):
        lo = f_min + c * por_col
        hi = min(lo + por_col, f_max)
        mejor = 0.0
        for f in range(lo, hi):
            if f in picos:
                mejor = max(mejor, picos[f] - suelo)
        columnas.append(mejor)

    return columnas


def perfil_espectro(margenes: list) -> str:
    """Perfil instantaneo con caracteres de altura creciente."""
    salida = []
    for m in margenes:
        # 24 dB de margen cubre el rango util observado: el WiFi cercano
        # ronda los 15-22 dB sobre el ruido.
        nivel = int(min(1.0, max(0.0, m) / 24.0) * (len(BLOQUES) - 1))
        salida.append(BLOQUES[nivel])
    return "".join(salida)


def fila_cascada(col: Color, margenes: list) -> str:
    """Una fila de cascada: un caracter y un color por columna."""
    trozos = []
    actual = None
    buffer = []

    for m in margenes:
        for corte, caracter, nombre in CASCADA_NIVELES:
            if m >= corte:
                break
        # Se agrupan caracteres consecutivos del mismo color en un solo
        # tramo ANSI. Sin esto, una fila de 90 columnas serian 90 secuencias
        # de escape y el repintado parpadearia en un terminal por serie.
        if nombre != actual:
            if buffer:
                trozos.append(col.por_nombre(actual, "".join(buffer)))
            actual, buffer = nombre, []
        buffer.append(caracter)

    if buffer:
        trozos.append(col.por_nombre(actual, "".join(buffer)))
    return "".join(trozos)


def eje_frecuencias(f_min: int, f_max: int, ancho: int) -> str:
    """Regla de frecuencias bajo el espectro, con marcas intermedias."""
    if ancho <= 0 or f_max <= f_min:
        return ""

    n_marcas = max(2, min(6, ancho // 14))
    linea = [" "] * ancho

    for i in range(n_marcas):
        pos = int(i * (ancho - 1) / (n_marcas - 1))
        f = f_min + round(i * (f_max - f_min) / (n_marcas - 1))
        etiqueta = str(f)
        # La primera marca se alinea a la izquierda y la ultima a la
        # derecha; las de en medio se centran sobre su posicion.
        if i == 0:
            ini = 0
        elif i == n_marcas - 1:
            ini = ancho - len(etiqueta)
        else:
            ini = pos - len(etiqueta) // 2
        ini = max(0, min(ancho - len(etiqueta), ini))
        for k, c in enumerate(etiqueta):
            linea[ini + k] = c

    return "".join(linea)


def desde(segundos: float) -> str:
    if segundos < 0:
        return "--"
    s = int(segundos)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h"


def etiqueta_emision(e: dict) -> str:
    """Texto corto y estable para el registro y la lista de emisiones."""
    banda = f"{e['f_inicio_mhz']}–{e['f_fin_mhz']} MHz"
    if e["clase"] == "continua":
        if e["tipo"] == "wifi":
            return f"{banda}  WiFi {e.get('detalle', '')}".strip()
        return f"{banda}  {e['tipo']}"
    return f"{banda}  salto de frecuencia"


def clave_emision(e: dict) -> tuple:
    """Identidad de una emision entre ciclos.

    Solo la banda y la clase. La potencia y la persistencia fluctuan en
    cada medida, y meterlas aqui haria que la misma emision contara como
    nueva en cada ciclo, inundando el registro.
    """
    return (e["f_inicio_mhz"], e["f_fin_mhz"], e["clase"])


# -------------------------------------------------------------------- salud

class Salud:
    """Estado de la plataforma, leido con cuentagotas.

    Las lecturas de /proc y /sys son baratas; `vcgencmd` y `systemctl` son
    procesos externos y no tienen por que ejecutarse en cada refresco. Se
    cachean, porque nada de lo que informan cambia en un segundo y medio.
    """

    UNIDADES = ("guard-detector-rf", "guard-bridge")

    def __init__(self):
        self._cache = {}
        self._ts = 0.0

    def leer(self) -> dict:
        if time.monotonic() - self._ts < SALUD_TTL_S and self._cache:
            return self._cache

        d = {"temp_c": None, "carga": None, "uptime_s": None,
             "estrangulado": None, "servicios": {}}

        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                d["temp_c"] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            pass

        try:
            with open("/proc/loadavg") as f:
                d["carga"] = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        try:
            with open("/proc/uptime") as f:
                d["uptime_s"] = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        # Bit 2 de get_throttled = estrangulamiento activo ahora mismo;
        # bit 18 = ha ocurrido desde el arranque. Interesan los dos: el
        # primero explica una medida mala en curso, el segundo delata un
        # equipo que ya se calento aunque ahora este frio.
        salida = self._ejecutar(["vcgencmd", "get_throttled"])
        if salida and "=" in salida:
            try:
                v = int(salida.split("=")[1].strip(), 16)
                d["estrangulado"] = (bool(v & 0x4), bool(v & 0x40000))
            except ValueError:
                pass

        for unidad in self.UNIDADES:
            estado = self._ejecutar(["systemctl", "is-active", unidad])
            d["servicios"][unidad] = estado or "?"

        self._cache = d
        self._ts = time.monotonic()
        return d

    @staticmethod
    def _ejecutar(cmd: list) -> str:
        """Nunca deja que una herramienta ausente tumbe el panel."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""


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

def bloque_cabecera(col: Color, args, analisis: dict, emisiones: list,
                    lista: list, estado_txt: str, ancho: int,
                    fuente: str, antiguedad: float,
                    ultima_deteccion: float) -> list:
    ahora = datetime.now().strftime("%H:%M:%S")
    titulo = " GUARD — panel de estado "
    relleno = ancho - len(titulo) - len(ahora) - 4

    def fila(etiqueta: str, izq: str, der: str = "") -> str:
        return f"  {etiqueta:<14}{izq:<26}{der}"

    ult = (desde(time.monotonic() - ultima_deteccion)
           if ultima_deteccion > 0 else "--")
    origen = ("detector (guard_rf)" if fuente == "detector"
              else "barrido propio")
    if fuente == "detector" and antiguedad:
        origen += f" · {antiguedad:.0f} s"

    return [
        col.fuerte(f"┌─{titulo}" + "─" * max(0, relleno) + f" {ahora} ─┐"),
        "",
        f"  {'ESTADO':<14}{estado_txt}",
        fila("banda", f"{args.f_min}–{args.f_max} MHz",
             f"suelo   {analisis['suelo_ruido_db']} dBFS"),
        fila("origen", origen,
             f"umbral  {analisis['umbral_db']} dBFS"),
        fila("lista blanca", f"{len(lista)} emisor(es)",
             f"última alerta hace {ult}"),
    ]


def bloque_salud(col: Color, s: dict) -> list:
    partes = []

    if s["temp_c"] is not None:
        t = s["temp_c"]
        txt = f"{t:.0f} °C"
        # 80 grados es donde el benchmark empezo a ver degradacion; 70 es
        # el aviso para tenerlo a la vista antes de que pase.
        partes.append(col.rojo(txt) if t >= 80
                      else col.amarillo(txt) if t >= 70
                      else col.verde(txt))

    if s["estrangulado"] is not None:
        ahora, alguna_vez = s["estrangulado"]
        if ahora:
            partes.append(col.rojo("estrangulado AHORA"))
        elif alguna_vez:
            partes.append(col.amarillo("estranguló antes"))
        else:
            partes.append(col.gris("sin estrangular"))

    if s["carga"] is not None:
        partes.append(col.gris(f"carga {s['carga']:.2f}"))

    if s["uptime_s"] is not None:
        partes.append(col.gris(f"activo {desde(s['uptime_s'])}"))

    for unidad, estado in s["servicios"].items():
        corto = unidad.replace("guard-", "")
        partes.append(col.verde(corto) if estado == "active"
                      else col.rojo(f"{corto}: {estado}"))

    if not partes:
        return []
    return ["", f"  {'PLATAFORMA':<14}" + col.gris(" · ").join(partes)]


def bloque_espectro(col: Color, args, margenes: list, ancho: int) -> list:
    perfil = perfil_espectro(margenes).ljust(len(margenes))
    sangria = " " * 2
    return [
        "",
        col.gris("  ESPECTRO   (altura = margen sobre el ruido)"),
        sangria + col.azul(perfil),
        sangria + col.gris(eje_frecuencias(args.f_min, args.f_max,
                                           len(margenes))),
    ]


def bloque_cascada(col: Color, historial: deque, filas: int,
                   columnas: int) -> list:
    if filas <= 0 or not historial:
        return []

    # La mas reciente arriba: la vista natural de una cascada es que lo
    # nuevo entra por donde esta la mirada, junto al perfil instantaneo.
    recientes = list(historial)[-filas:][::-1]
    lineas = ["", col.gris(f"  RECIENTE   (últimos {len(recientes)} ciclos, "
                           f"el más nuevo arriba)")]
    for margenes in recientes:
        # Si el terminal ha cambiado de ancho, las filas guardadas antes
        # tienen otra longitud. Se ajustan al ancho actual para que la
        # cascada no quede dentada tras un redimensionado.
        if len(margenes) != columnas:
            margenes = (margenes[:columnas] if len(margenes) > columnas
                        else margenes + [0.0] * (columnas - len(margenes)))
        lineas.append("  " + fila_cascada(col, margenes))
    return lineas


def bloque_emisiones(col: Color, emisiones: list, desconocidas: list,
                     maximo: int) -> list:
    if not emisiones:
        return ["", col.gris("  Sin emisiones por encima del umbral.")]

    # Las no identificadas primero, y dentro de cada grupo las de mayor
    # margen: si no caben todas, las que se pierden son las que menos
    # importan.
    orden = sorted(emisiones,
                   key=lambda e: (bool(e.get("conocido")), -e["snr_db"]))
    mostradas = orden[:maximo] if maximo > 0 else []

    lineas = ["", col.gris(f"  EMISIONES   ({len(emisiones)}, "
                           f"{len(desconocidas)} sin identificar)")]

    for e in mostradas:
        if e.get("conocido"):
            marca = col.verde("CONOCIDA")
            barra = col.gris(barra_snr(e["snr_db"], ancho=18))
        else:
            marca = col.rojo(col.fuerte("NO IDENTIFICADA"))
            barra = col.rojo(barra_snr(e["snr_db"], ancho=18))

        lineas.append(
            f"  {etiqueta_emision(e):<36}{barra}  "
            f"+{e['snr_db']:.1f} dB  pers. {e['persistencia'] * 100:.0f} %  "
            f"{marca}")

    ocultas = len(orden) - len(mostradas)
    if ocultas > 0:
        lineas.append(col.gris(f"  … y {ocultas} más (pantalla corta)"))
    return lineas


def bloque_registro(col: Color, registro: deque, filas: int) -> list:
    if filas <= 0 or not registro:
        return []
    recientes = list(registro)[-filas:][::-1]
    lineas = ["", col.gris("  REGISTRO   (emisiones nuevas)")]
    for hora, texto, conocida in recientes:
        marca = col.gris("·") if conocida else col.rojo("!")
        lineas.append(f"  {col.gris(hora)} {marca} {texto}")
    return lineas


def pintar(col: Color, args, analisis: dict, emisiones: list, lista: list,
           historial: deque, registro: deque, salud: dict,
           ultima_deteccion: float, ciclo: int,
           fuente: str = "propia", antiguedad: float = 0.0) -> None:
    term = shutil.get_terminal_size((100, 30))
    ancho = max(60, min(term.columns - 2, 120))
    alto = max(16, term.lines)

    desconocidas = [e for e in emisiones if not e.get("conocido")]
    obsoleto = (fuente == "detector" and antiguedad > ANTIGUEDAD_MAX_S)

    # --- estado, replicando la maquina de estados del ESP32
    # Un snapshot obsoleto se trata como perdida de enlace: preferible
    # declarar que no hay informacion fiable a mostrar datos viejos como
    # si fueran actuales.
    if obsoleto:
        estado_txt = col.rojo(col.fuerte(
            f"● DETECTOR CAIDO — sin datos desde hace {desde(antiguedad)}"))
    elif analisis.get("receptor") == "sin_datos":
        # El detector esta vivo y publica: lo que falla es el receptor.
        estado_txt = col.rojo(col.fuerte("● RECEPTOR SIN SEÑAL — no se vigila"))
    elif analisis["n_barridos"] == 0:
        estado_txt = col.amarillo("● ESPERANDO AL DETECTOR")
    elif desconocidas:
        estado_txt = col.rojo(col.fuerte("● ALERTA — actividad no identificada"))
    else:
        estado_txt = col.verde("● OPERATIVO — sin actividad no identificada")

    margenes = margenes_por_columna(
        analisis["picos"], analisis["suelo_ruido_db"],
        args.f_min, args.f_max, ancho - 4)

    partes = bloque_cabecera(col, args, analisis, emisiones, lista,
                             estado_txt, ancho, fuente, antiguedad,
                             ultima_deteccion)
    partes += bloque_salud(col, salud)
    partes += bloque_espectro(col, args, margenes, ancho)

    # --- reparto del alto disponible
    #
    # El panel tiene que caber en la pantalla que haya, que en el equipo
    # final sera de 5 a 7 pulgadas. En lugar de recortar por abajo —lo que
    # esconderia justo el registro de alertas— se reparte lo que queda:
    # primero las emisiones, que son el dato operativo; luego la cascada,
    # que es contexto; y el registro solo si aun sobra sitio.
    fijo = len(partes) + 2          # + pie y margen
    libre = max(0, alto - fijo)

    max_emisiones = min(len(emisiones), max(0, (libre - 2) // 1)) if emisiones else 0
    max_emisiones = min(max_emisiones, 6)
    bloque_em = bloque_emisiones(col, emisiones, desconocidas, max_emisiones)
    partes += bloque_em
    libre -= len(bloque_em)

    if not args.sin_cascada and libre > 4:
        filas = min(CASCADA_MAX, libre - 3)
        bloque_ca = bloque_cascada(col, historial, filas, len(margenes))
        partes += bloque_ca
        libre -= len(bloque_ca)

    if libre > 3:
        bloque_re = bloque_registro(col, registro, libre - 3)
        partes += bloque_re

    partes.append("")
    partes.append(col.gris(f"  ciclo {ciclo}   ·   Ctrl+C para salir"))

    sys.stdout.write(col.inicio() + "\n".join(partes) + "\n" +
                     col.borrar_resto())
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
    p.add_argument("--sin-cascada", action="store_true",
                   help="oculta la cascada (pantallas de muy poca altura)")
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
    salud = Salud()

    historial = deque(maxlen=CASCADA_MAX)
    registro = deque(maxlen=REGISTRO_MAX)
    presentes = set()

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
                    analisis = {"n_barridos": 0, "receptor": "ok",
                                "suelo_ruido_db": 0.0, "umbral_db": 0.0,
                                "persistencia": {}, "picos": {}}
                    emisiones, fuente, antiguedad = [], "detector", 0.0
                else:
                    analisis, emisiones, antiguedad = capturar_propio(args, lista)
                    fuente = "propia"

            if not _running:
                break

            # Solo cuenta como medida nueva si el analisis ha cambiado; de
            # lo contrario un snapshot estancado llenaria la cascada de
            # copias de la misma foto y el registro de falsas novedades.
            ts_actual = (analisis["n_barridos"], analisis["suelo_ruido_db"],
                         len(emisiones))
            medida_nueva = ts_actual != ts_previo
            ts_previo = ts_actual

            if medida_nueva:
                term = shutil.get_terminal_size((100, 30))
                historial.append(margenes_por_columna(
                    analisis["picos"], analisis["suelo_ruido_db"],
                    args.f_min, args.f_max,
                    max(60, min(term.columns - 2, 120)) - 4))

                claves = {clave_emision(e): e for e in emisiones}
                hora = datetime.now().strftime("%H:%M:%S")
                for clave, e in claves.items():
                    if clave not in presentes:
                        registro.append((hora, etiqueta_emision(e),
                                         bool(e.get("conocido"))))
                presentes = set(claves)

                if any(not e.get("conocido") for e in emisiones):
                    ultima_deteccion = time.monotonic()

            pintar(col, args, analisis, emisiones, lista, historial, registro,
                   salud.leer(), ultima_deteccion, ciclo, fuente, antiguedad)

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
