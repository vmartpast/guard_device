#!/usr/bin/env python3
"""guard_panel — panel de estado en terminal, navegable con joystick.

Es la interfaz principal del dispositivo. El ESP32 queda como periferico
de entrada y aviso —joystick, zumbador, LEDs— y el menu vive aqui: una
sola pantalla que leer y un solo mando con el que moverse.

Reutiliza integramente la logica de analisis de guard_rf.py; este modulo
solo presenta. Si el analisis cambia, el panel lo hereda.

Que muestra, y por que
----------------------
Un panel que solo muestra el instante obliga a quien mira a estar
mirando. Por eso hay tres horizontes:

    ahora       estado, emisiones y perfil del espectro
    reciente    cascada: las ultimas N medidas, una fila por ciclo
    historico   registro de emisiones nuevas, con la hora

La cascada es lo que convierte el panel en un instrumento: una emision
intermitente no se distingue de un pico de ruido en una sola foto, pero
en quince filas apiladas se ve sola.

La franja de salud de la plataforma esta por la misma razon por la que
existe el estado 'receptor sin senal': un equipo desatendido que se
degrada en silencio es peor que uno que falla ruidosamente.

Navegacion
----------
El puente escribe la ultima pulsacion en /run/guard/input.json con un
contador que crece. El panel mira ese fichero diez veces por segundo y
repinta en cuanto cambia, mientras el analisis se refresca a su propio
ritmo. Separar las dos cadencias es lo que hace que el menu responda al
instante aunque una medida tarde segundos.

El contrato es un fichero y no un socket a proposito: se puede ejercitar
la interfaz entera sin el ESP32 conectado, escribiendo a mano en el.

    echo '{"seq":1,"accion":"abajo"}' > /run/guard/input.json

Uso:
  guard_panel.py                     panel en vivo, banda de 2,4 GHz
  guard_panel.py --f-min 5100 --f-max 5900
  guard_panel.py --sin-color                     terminales sin ANSI
  guard_panel.py --sin-cascada                   pantallas muy bajas

Se detiene con Ctrl+C.
"""

import argparse
import json
import os
import re
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

CASCADA_MAX = 24
REGISTRO_MAX = 60

# Un snapshot mas antiguo que esto significa que el detector no esta
# publicando: se muestra el dato con aviso en lugar de fingir que es actual.
ANTIGUEDAD_MAX_S = 30.0

# La salud de la plataforma cambia despacio y su lectura cuesta un proceso
# externo. Se refresca cada pocos segundos, no en cada pintado.
SALUD_TTL_S = 10.0

# Pulsaciones publicadas por el puente.
ENTRADA_FILE = Path("/run/guard/input.json")

# Cadencias. La entrada se mira mucho mas a menudo que el analisis: un
# menu que responde cada segundo y medio se siente roto.
PERIODO_ENTRADA_S = 0.1
PERIODO_DATOS_S = 1.5

ANCHO_MENU = 18

# Orden del menu. La primera es la vista de arranque y la de inicio.
VISTAS = [
    ("ESTADO", "estado"),
    ("ESPECTRO", "espectro"),
    ("EMISIONES", "emisiones"),
    ("LISTA BLANCA", "lista"),
    ("SISTEMA", "sistema"),
    ("REGISTRO", "registro"),
]

_running = True


def _stop(signum, frame):
    global _running
    _running = False


# -------------------------------------------------------------------- color

_ANSI = re.compile(r"\033\[[0-9;]*m")


def ancho_visible(texto: str) -> int:
    """Longitud en pantalla, ignorando las secuencias de escape.

    Hace falta para componer dos columnas: len() cuenta los codigos de
    color como caracteres y desplazaria el menu varias posiciones por
    cada tramo coloreado de la izquierda.
    """
    return len(_ANSI.sub("", texto))


def rellenar(texto: str, ancho: int) -> str:
    falta = ancho - ancho_visible(texto)
    return texto + " " * falta if falta > 0 else texto


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
    def inverso(self, t):  return self._c("7", t)

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

def barra_snr(snr: float, ancho: int = 18, snr_max: float = 30.0) -> str:
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
        # tramo ANSI. Sin esto, una fila de 90 columnas serian 90
        # secuencias de escape y el repintado parpadearia en un terminal
        # por serie.
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


# ------------------------------------------------------------------ entrada

class Entrada:
    """Pulsaciones del joystick, publicadas por el puente.

    El contrato es un fichero con un contador que crece, no un socket ni
    una tuberia. Un socket obligaria a coordinar el arranque de los dos
    procesos y una tuberia bloquearia al puente si el panel no esta
    leyendo. Un fichero no tiene ninguno de esos problemas, se puede
    inspeccionar con `cat` y se puede falsear con `echo` para ejercitar
    la interfaz sin el ESP32 delante.
    """

    def __init__(self):
        self._seq = None

    def leer(self):
        try:
            with open(ENTRADA_FILE) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        seq = d.get("seq")
        if seq is None or seq == self._seq:
            return None

        primera = self._seq is None
        self._seq = seq
        # En la primera lectura solo se toma nota del contador. Si el
        # fichero ya existia de antes, ejecutar esa pulsacion al arrancar
        # movaria el menu solo, sin que nadie haya tocado el mando.
        return None if primera else d.get("accion")


class Menu:
    """Estado de navegacion.

    Mover el cursor cambia la vista en el acto, sin confirmar. Un menu de
    instrumento no gana nada con un paso de confirmacion: la vista ES la
    seleccion, y asi no hay dos estados (cursor y vista activa) que
    puedan desincronizarse.
    """

    def __init__(self):
        self.i = 0
        self.pausado = False

    def accion(self, a: str) -> bool:
        """Devuelve True si algo cambio y hay que repintar."""
        if a == "arriba":
            self.i = (self.i - 1) % len(VISTAS)
        elif a == "abajo":
            self.i = (self.i + 1) % len(VISTAS)
        elif a == "ok":
            self.pausado = not self.pausado
        elif a == "atras":
            if self.i == 0 and not self.pausado:
                return False
            self.i, self.pausado = 0, False
        else:
            return False
        return True

    @property
    def vista(self) -> str:
        return VISTAS[self.i][1]


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
             "estrangulado": None, "servicios": {}, "memoria": None}

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

        try:
            info = {}
            with open("/proc/meminfo") as f:
                for linea in f:
                    k, _, v = linea.partition(":")
                    info[k] = int(v.split()[0])
            d["memoria"] = (info["MemTotal"] - info["MemAvailable"],
                            info["MemTotal"])
        except (OSError, ValueError, KeyError, IndexError):
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
            d["servicios"][unidad] = self._ejecutar(
                ["systemctl", "is-active", unidad]) or "?"

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


# --------------------------------------------------------------------- menu

def bloque_menu(col: Color, menu: Menu, alto: int) -> list:
    lineas = [col.gris(" │")]
    for i, (nombre, _) in enumerate(VISTAS):
        if i == menu.i:
            lineas.append(col.gris(" │ ") +
                          col.fuerte(col.azul(f"▸ {nombre}")))
        else:
            lineas.append(col.gris(f" │   {nombre}"))

    lineas.append(col.gris(" │"))
    lineas.append(col.gris(" │ ▲▼  vista"))
    lineas.append(col.gris(" │ OK  " +
                           ("reanudar" if menu.pausado else "pausar")))
    lineas.append(col.gris(" │ ◀   inicio"))

    while len(lineas) < alto:
        lineas.append(col.gris(" │"))
    return lineas[:alto]


# ------------------------------------------------------------------- vistas

def cabecera(col: Color, args, analisis: dict, emisiones: list, lista: list,
             estado_txt: str, ancho: int, fuente: str, antiguedad: float,
             ultima_deteccion: float, pausado: bool) -> list:
    ahora = datetime.now().strftime("%H:%M:%S")
    marca = col.amarillo(" PAUSA ") if pausado else ahora
    titulo = " GUARD — panel de estado "
    relleno = ancho - len(titulo) - ancho_visible(marca) - 4

    def fila(etiqueta: str, izq: str, der: str = "") -> str:
        return f"  {etiqueta:<14}{izq:<26}{der}"

    ult = (desde(time.monotonic() - ultima_deteccion)
           if ultima_deteccion > 0 else "--")
    origen = ("detector" if fuente == "detector" else "barrido propio")
    if fuente == "detector" and antiguedad:
        origen += f" · {antiguedad:.0f} s"

    return [
        col.fuerte(f"┌─{titulo}" + "─" * max(0, relleno) + f" {marca} ─┐"),
        "",
        f"  {'ESTADO':<14}{estado_txt}",
        fila("banda", f"{args.f_min}–{args.f_max} MHz",
             f"suelo   {analisis['suelo_ruido_db']} dBFS"),
        fila("origen", origen,
             f"umbral  {analisis['umbral_db']} dBFS"),
        fila("lista blanca", f"{len(lista)} emisor(es)",
             f"última alerta hace {ult}"),
        "",
    ]


def vista_estado(col, args, datos, ancho, alto) -> list:
    """Resumen: espectro, emisiones destacadas y algo de historia."""
    lineas = bloque_espectro(col, args, datos["margenes"], compacto=True)
    lineas += bloque_emisiones(col, datos, maximo=4, detalle=False,
                               ancho=ancho)

    libre = alto - len(lineas) - 1
    if not args.sin_cascada and libre > 3:
        lineas += bloque_cascada(col, datos, min(8, libre - 2))
    return lineas


def vista_espectro(col, args, datos, ancho, alto) -> list:
    """Todo el alto para el espectro y su evolucion."""
    lineas = bloque_espectro(col, args, datos["margenes"], compacto=False)
    libre = alto - len(lineas) - 1
    if libre > 3:
        lineas += bloque_cascada(col, datos, min(CASCADA_MAX, libre - 2))
    lineas.append("")
    lineas.append(col.gris("  ░ 4-8 dB    ▒ 8-14 dB    ▓ 14-20 dB    █ >20 dB"))
    return lineas


def vista_emisiones(col, args, datos, ancho, alto) -> list:
    return bloque_emisiones(col, datos, maximo=max(1, (alto - 4) // 3),
                            detalle=True, ancho=ancho)


def vista_lista(col, args, datos, ancho, alto) -> list:
    lista = datos["lista"]
    lineas = [col.gris("  LISTA BLANCA   (emisores declarados del entorno)"), ""]
    if not lista:
        lineas.append(col.amarillo("  Vacía: toda emisión se reporta como no "
                                   "identificada."))
        lineas.append("")
        lineas.append(col.gris("  Se genera con:  guard_rf.py --calibrar"))
        return lineas

    lineas.append(f"  {'banda':<18}{'clase':<20}{'etiqueta'}")
    lineas.append(col.gris("  " + "─" * (ancho - 4)))
    for e in lista[:max(1, alto - 8)]:
        banda = f"{e.get('f_inicio_mhz', '?')}–{e.get('f_fin_mhz', '?')} MHz"
        lineas.append(f"  {banda:<18}{e.get('clase', 'cualquiera'):<20}"
                      f"{e.get('etiqueta', 'sin etiqueta')}")

    lineas.append("")
    lineas.append(col.gris("  Una entrada solo silencia emisiones de su misma "
                           "clase: un WiFi declarado"))
    lineas.append(col.gris("  no enmascara una FHSS que comparta banda."))
    return lineas


def vista_sistema(col, args, datos, ancho, alto) -> list:
    s = datos["salud"]
    lineas = [col.gris("  PLATAFORMA"), ""]

    def fila(k, v):
        return f"  {k:<22}{v}"

    if s["temp_c"] is not None:
        t = s["temp_c"]
        # 80 grados es donde el benchmark empezo a ver degradacion; 70 es
        # el aviso para tenerlo a la vista antes de que pase.
        txt = (col.rojo(f"{t:.1f} °C") if t >= 80
               else col.amarillo(f"{t:.1f} °C") if t >= 70
               else col.verde(f"{t:.1f} °C"))
        lineas.append(fila("temperatura", txt))

    if s["estrangulado"] is not None:
        ahora, alguna_vez = s["estrangulado"]
        lineas.append(fila("estrangulamiento",
                           col.rojo("activo ahora mismo") if ahora
                           else col.amarillo("ocurrió desde el arranque")
                           if alguna_vez else col.verde("nunca")))

    if s["carga"] is not None:
        lineas.append(fila("carga media (1 min)", f"{s['carga']:.2f}"))

    if s["memoria"]:
        usada, total = s["memoria"]
        lineas.append(fila("memoria",
                           f"{usada // 1024} / {total // 1024} MB"))

    if s["uptime_s"] is not None:
        lineas.append(fila("activo desde hace", desde(s["uptime_s"])))

    lineas.append("")
    lineas.append(col.gris("  SERVICIOS"))
    lineas.append("")
    for unidad, estado in s["servicios"].items():
        lineas.append(fila(unidad,
                           col.verde(estado) if estado == "active"
                           else col.rojo(estado)))

    lineas.append("")
    lineas.append(col.gris("  CONFIGURACIÓN"))
    lineas.append("")
    lineas.append(fila("banda", f"{args.f_min}–{args.f_max} MHz"))
    lineas.append(fila("ancho de bin", f"{args.ancho_bin // 1000} kHz"))
    lineas.append(fila("margen de detección", f"{args.margen} dB"))
    lineas.append(fila("barridos por análisis",
                       str(datos["analisis"]["n_barridos"])))
    return lineas


def vista_registro(col, args, datos, ancho, alto) -> list:
    registro = datos["registro"]
    lineas = [col.gris("  REGISTRO   (emisiones nuevas, la más reciente "
                       "arriba)"), ""]
    if not registro:
        lineas.append(col.gris("  Todavía no se ha registrado ninguna."))
        return lineas
    for hora, texto, conocida in list(registro)[-(alto - 4):][::-1]:
        marca = col.gris("·") if conocida else col.rojo("!")
        etiqueta = col.gris(texto) if conocida else texto
        lineas.append(f"  {col.gris(hora)}  {marca}  {etiqueta}")
    return lineas


VISTAS_FN = {
    "estado": vista_estado,
    "espectro": vista_espectro,
    "emisiones": vista_emisiones,
    "lista": vista_lista,
    "sistema": vista_sistema,
    "registro": vista_registro,
}


# ------------------------------------------------------------------ bloques

def bloque_espectro(col: Color, args, margenes: list,
                    compacto: bool) -> list:
    titulo = ("  ESPECTRO" if compacto
              else "  ESPECTRO   (altura = margen sobre el ruido)")
    return [
        col.gris(titulo),
        "  " + col.azul(perfil_espectro(margenes)),
        "  " + col.gris(eje_frecuencias(args.f_min, args.f_max,
                                        len(margenes))),
        "",
    ]


def bloque_cascada(col: Color, datos: dict, filas: int) -> list:
    historial, columnas = datos["historial"], len(datos["margenes"])
    if filas <= 0 or not historial:
        return []

    # La mas reciente arriba: la vista natural de una cascada es que lo
    # nuevo entra por donde esta la mirada, junto al perfil instantaneo.
    recientes = list(historial)[-filas:][::-1]
    lineas = [col.gris(f"  RECIENTE   (últimos {len(recientes)} ciclos, "
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


def bloque_emisiones(col: Color, datos: dict, maximo: int,
                     detalle: bool, ancho: int) -> list:
    emisiones = datos["emisiones"]
    desconocidas = [e for e in emisiones if not e.get("conocido")]

    if not emisiones:
        return [col.gris("  Sin emisiones por encima del umbral."), ""]

    # Las no identificadas primero, y dentro de cada grupo las de mayor
    # margen: si no caben todas, las que se pierden son las que menos
    # importan.
    orden = sorted(emisiones,
                   key=lambda e: (bool(e.get("conocido")), -e["snr_db"]))
    mostradas = orden[:max(0, maximo)]

    # El ancho de la etiqueta se calcula, no se fija: si desbordara la
    # columna de contenido, empujaria el menu de la derecha y la pantalla
    # quedaria dentada linea a linea.
    w_barra = 14
    w_et = max(20, ancho - w_barra - 41)

    lineas = [col.gris(f"  EMISIONES   ({len(emisiones)}, "
                       f"{len(desconocidas)} sin identificar)"), ""]

    for e in mostradas:
        if e.get("conocido"):
            marca = col.verde("CONOCIDA")
            barra = col.gris(barra_snr(e["snr_db"], ancho=w_barra))
        else:
            marca = col.rojo(col.fuerte("NO IDENTIFICADA"))
            barra = col.rojo(barra_snr(e["snr_db"], ancho=w_barra))

        lineas.append(f"  {etiqueta_emision(e)[:w_et]:<{w_et}}{barra}  "
                      f"+{e['snr_db']:.1f} dB  "
                      f"pers. {e['persistencia'] * 100:.0f} %  {marca}")

        if detalle:
            extra = (f"pico {e['pico_db']} dBFS · ancho {e['ancho_mhz']} MHz "
                     f"· centro {e['f_centro_mhz']} MHz")
            if e["clase"] != "continua" and "canales_visitados" in e:
                extra += f" · {e['canales_visitados']} canales visitados"
            if e.get("etiqueta"):
                extra += f" · «{e['etiqueta']}»"
            lineas.append(col.gris(f"    {extra[:ancho - 6]}"))
            lineas.append("")

    ocultas = len(orden) - len(mostradas)
    if ocultas > 0:
        lineas.append(col.gris(f"  … y {ocultas} más (no caben en pantalla)"))
    if not detalle:
        lineas.append("")
    return lineas


# ------------------------------------------------------------------- pintado

def pintar(col: Color, args, datos: dict, menu: Menu, ciclo: int) -> None:
    term = shutil.get_terminal_size((100, 30))
    ancho = max(70, min(term.columns - 2, 130))
    alto = max(16, term.lines)
    ancho_c = ancho - ANCHO_MENU

    analisis = datos["analisis"]
    emisiones = datos["emisiones"]
    desconocidas = [e for e in emisiones if not e.get("conocido")]
    obsoleto = (datos["fuente"] == "detector"
                and datos["antiguedad"] > ANTIGUEDAD_MAX_S)

    # --- estado, replicando la maquina de estados que tenia el ESP32
    # Un snapshot obsoleto se trata como perdida de enlace: preferible
    # declarar que no hay informacion fiable a mostrar datos viejos como
    # si fueran actuales.
    if obsoleto:
        estado_txt = col.rojo(col.fuerte(
            f"● DETECTOR CAIDO — sin datos desde hace "
            f"{desde(datos['antiguedad'])}"))
    elif analisis.get("receptor") == "sin_datos":
        # El detector esta vivo y publica: lo que falla es el receptor.
        estado_txt = col.rojo(col.fuerte("● RECEPTOR SIN SEÑAL — no se vigila"))
    elif analisis["n_barridos"] == 0:
        estado_txt = col.amarillo("● ESPERANDO AL DETECTOR")
    elif desconocidas:
        estado_txt = col.rojo(col.fuerte("● ALERTA — actividad no identificada"))
    else:
        estado_txt = col.verde("● OPERATIVO — sin actividad no identificada")

    # La cabecera y el estado salen en TODAS las vistas. Una alerta no
    # puede depender de en que pantalla este el operador.
    contenido = cabecera(col, args, analisis, emisiones, datos["lista"],
                         estado_txt, ancho_c, datos["fuente"],
                         datos["antiguedad"], datos["ultima_deteccion"],
                         menu.pausado)

    alto_vista = alto - len(contenido) - 2
    contenido += VISTAS_FN[menu.vista](col, args, datos, ancho_c, alto_vista)

    menu_lineas = bloque_menu(col, menu, len(contenido))

    salida = []
    for i in range(max(len(contenido), len(menu_lineas))):
        izq = contenido[i] if i < len(contenido) else ""
        der = menu_lineas[i] if i < len(menu_lineas) else ""
        salida.append(rellenar(izq, ancho_c) + der)

    salida.append("")
    salida.append(col.gris(f"  ciclo {ciclo}   ·   Ctrl+C para salir"))

    sys.stdout.write(col.inicio() + "\n".join(salida) + "\n" +
                     col.borrar_resto())
    sys.stdout.flush()


# ---------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Panel de estado en terminal")
    p.add_argument("--f-min", type=int, default=2400)
    p.add_argument("--f-max", type=int, default=2500)
    p.add_argument("--ancho-bin", type=int, default=1_000_000)
    p.add_argument("--barridos", type=int, default=8,
                   help="barridos por ciclo en modo autonomo (def. 8)")
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
    salud = Salud()
    entrada = Entrada()
    menu = Menu()

    datos = {
        "analisis": {"n_barridos": 0, "receptor": "ok", "suelo_ruido_db": 0.0,
                     "umbral_db": 0.0, "persistencia": {}, "picos": {}},
        "emisiones": [],
        "lista": cargar_lista_blanca(args.config),
        "historial": deque(maxlen=CASCADA_MAX),
        "registro": deque(maxlen=REGISTRO_MAX),
        "salud": salud.leer(),
        "margenes": [],
        "fuente": "detector",
        "antiguedad": 0.0,
        "ultima_deteccion": 0.0,
    }

    presentes = set()
    ts_previo = None
    ciclo = 0
    proximo_dato = 0.0

    sys.stdout.write(col.limpiar() + col.cursor(False))
    sys.stdout.flush()

    try:
        while _running:
            repintar = False

            # --- entrada: se mira a 10 Hz, independientemente del analisis
            accion = entrada.leer()
            if accion and menu.accion(accion):
                repintar = True

            # --- datos: a su propio ritmo, y detenidos si el operador paso
            if not menu.pausado and time.monotonic() >= proximo_dato:
                ciclo += 1
                actualizar_datos(args, datos, salud, presentes, ts_previo)
                ts_previo = datos["_ts"]
                presentes = datos["_presentes"]
                proximo_dato = time.monotonic() + (
                    PERIODO_DATOS_S if datos["fuente"] == "detector" else 0.0)
                repintar = True

            if repintar and _running:
                pintar(col, args, datos, menu, ciclo)

            if _running:
                time.sleep(PERIODO_ENTRADA_S)
    finally:
        sys.stdout.write(col.cursor(True))
        sys.stdout.write("\n  panel detenido\n")
        sys.stdout.flush()

    return 0


def actualizar_datos(args, datos: dict, salud: Salud, presentes: set,
                     ts_previo) -> None:
    """Toma una medida nueva y actualiza historial, registro y salud."""
    # El detector, si esta corriendo, es el unico que habla con el
    # receptor: el panel consume su analisis en lugar de competir por el
    # HackRF. Solo barre por su cuenta si no hay detector.
    analisis = emisiones = None
    fuente, antiguedad = "propia", 0.0

    if not args.autonomo:
        analisis, emisiones, antiguedad = leer_snapshot()
        if analisis is not None:
            fuente = "detector"

    if analisis is None:
        if args.sin_receptor:
            # Ejecutandose como servicio junto al detector: no debe abrir
            # el HackRF bajo ningun concepto, porque se lo quitaria.
            analisis = {"n_barridos": 0, "receptor": "ok",
                        "suelo_ruido_db": 0.0, "umbral_db": 0.0,
                        "persistencia": {}, "picos": {}}
            emisiones, fuente, antiguedad = [], "detector", 0.0
        else:
            analisis, emisiones, antiguedad = capturar_propio(
                args, datos["lista"])
            fuente = "propia"

    datos.update({"analisis": analisis, "emisiones": emisiones,
                  "fuente": fuente, "antiguedad": antiguedad,
                  "salud": salud.leer()})

    term = shutil.get_terminal_size((100, 30))
    ancho_c = max(70, min(term.columns - 2, 130)) - ANCHO_MENU
    datos["margenes"] = margenes_por_columna(
        analisis["picos"], analisis["suelo_ruido_db"],
        args.f_min, args.f_max, ancho_c - 4)

    # Solo cuenta como medida nueva si el analisis ha cambiado; de lo
    # contrario un snapshot estancado llenaria la cascada de copias de la
    # misma foto y el registro de falsas novedades.
    ts_actual = (analisis["n_barridos"], analisis["suelo_ruido_db"],
                 len(emisiones))
    datos["_ts"] = ts_actual
    datos["_presentes"] = presentes

    if ts_actual == ts_previo:
        return

    datos["historial"].append(list(datos["margenes"]))

    claves = {clave_emision(e): e for e in emisiones}
    hora = datetime.now().strftime("%H:%M:%S")
    for clave, e in claves.items():
        if clave not in presentes:
            datos["registro"].append((hora, etiqueta_emision(e),
                                      bool(e.get("conocido"))))
    datos["_presentes"] = set(claves)

    if any(not e.get("conocido") for e in emisiones):
        datos["ultima_deteccion"] = time.monotonic()


if __name__ == "__main__":
    sys.exit(main())
