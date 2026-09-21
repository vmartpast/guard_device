#!/usr/bin/env python3
"""guard_rf — detector de actividad radioelectrica no identificada.

Unifica el analisis de energia y el de persistencia temporal en un solo
pase sobre los datos. Responde, para cada emision detectada:

    donde       banda ocupada, ancho y potencia de pico
    que tipo    continua en frecuencia fija, o salto de frecuencia
    cual        canal WiFi normalizado, u otra cosa
    conocida    presente o no en la lista blanca

No identifica modelos de UAV ni distingue telemetria de video: eso
corresponde al vertical de procesado de senal. Este modulo detecta
*actividad no identificada*, que es lo que el requisito operativo del ET
pide filtrar mediante lista blanca.

Emite eventos conformes a INTEGRACION.md seccion 3.2, por lo que puede
sustituir al stub sin tocar el servicio puente ni el firmware.

Fundamento del metodo
---------------------
Un barrido acumulado no distingue una emision continua de una de salto de
frecuencia: con suficiente acumulacion, una FHSS ocupa la banda entera y
eleva el suelo de ruido estimado hasta cegar la deteccion. Medido en este
equipo: el suelo paso de -55 a -31 dB con Bluetooth activo, y ninguna
emision superaba ya el umbral, ni siquiera el WiFi detectado antes.

La solucion es tomar N barridos cortos en lugar de uno largo y medir, por
cada bin, en que fraccion de ellos aparece ocupado. El criterio que separa
ambas familias no es la frecuencia ni la potencia, sino la persistencia
temporal.

Uso:
  guard_rf.py                 un analisis, informe por consola
  guard_rf.py --mapa          anade el mapa de persistencia bin a bin
  guard_rf.py --continuo      servicio: analiza en bucle y emite eventos
  guard_rf.py --calibrar      propone lista blanca del entorno actual
  guard_rf.py --json          salida estructurada
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------- parametros

BANDA_MIN_MHZ = 2400
BANDA_MAX_MHZ = 2500
ANCHO_BIN_HZ = 1_000_000

N_BARRIDOS = 10
DURACION_BARRIDO_S = 1.5

# Margen sobre el suelo de ruido para considerar un bin ocupado en un
# barrido individual.
MARGEN_DB = 8.0

# Umbrales de persistencia, determinados experimentalmente:
#   WiFi con trafico real .... 40-80 %, irregular segun la carga del momento
#   Bluetooth activo ......... 10-12 %, repartido por toda la banda
# Un umbral alto fragmenta el WiFi en bins sueltos; la separacion real
# entre ambas familias esta en torno al 30 %.
UMBRAL_CONTINUA = 0.35
UMBRAL_ESPORADICA = 0.25

# Minimos para descartar ruido impulsivo.
MIN_BINS_CONTINUA = 3
MIN_BINS_FHSS = 6

# Canales WiFi de 2,4 GHz (centro en MHz). Nominalmente 20 MHz de ancho, de
# los que unos 10-14 superan un umbral situado 8-10 dB sobre el ruido: las
# faldas del canal quedan por debajo.
CANALES_WIFI = {
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432, 6: 2437,
    7: 2442, 8: 2447, 9: 2452, 10: 2457, 11: 2462, 12: 2467,
    13: 2472, 14: 2484,
}
TOLERANCIA_WIFI_MHZ = 4
ANCHO_WIFI_MIN = 8
ANCHO_WIFI_MAX = 18

HEALTH_FILE = Path("/run/guard/detector.health")
CONFIG_DEF = Path("/opt/guard_device/config/lista_blanca.json")

# Instantanea del ultimo analisis, para consumidores que necesitan mas que
# los eventos de alerta (el panel muestra tambien el suelo de ruido, las
# emisiones conocidas y el perfil completo del espectro).
#
# Vive en /run, que es tmpfs: no desgasta la tarjeta y se limpia sola en
# cada arranque, de modo que nunca se presenta como actual un analisis de
# la sesion anterior.
SNAPSHOT_FILE = Path("/run/guard/spectrum.json")

_running = True


def _stop(signum, frame):
    global _running
    _running = False


# ---------------------------------------------------------------- utilidades

def now_iso() -> str:
    ahora = datetime.now(timezone.utc)
    return ahora.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ahora.microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def touch_health() -> None:
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.touch()
    except OSError:
        pass


def mediana(valores: list) -> float:
    v = sorted(valores)
    n = len(v)
    if n == 0:
        return 0.0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def agrupar_contiguas(frecuencias: list) -> list:
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


# ------------------------------------------------------------------ captura

def _cerrar(proc) -> None:
    """Cierra hackrf_sweep sin dejarlo huerfano."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class Receptor:
    """Un unico hackrf_sweep vivo, troceado por tiempo en barridos.

    La primera version lanzaba un proceso hackrf_sweep por cada barrido,
    lo leia unos segundos y lo mataba. Con diez barridos por analisis eso
    son unas treinta y cinco aperturas y cierres del receptor por minuto,
    cada cierre un SIGTERM en mitad de una transferencia USB. El HackRF
    aguantaba alrededor de cuarenta ciclos y se quedaba atascado: dejaba
    de entregar datos sin devolver ningun error, y solo se recuperaba
    desconectando el cable. Medido en este equipo: el detector funcionaba
    algo mas de un minuto y medio y enmudecia.

    Aqui el proceso se abre una sola vez y no se vuelve a tocar. Un hilo
    lector consume su salida sin parar y acumula el maximo por bin; el
    bucle principal toma una foto de ese acumulador cada `duracion`
    segundos y la vacia. Cada foto es un barrido, con la misma semantica
    que antes —maximo por bin en una ventana de tiempo—, de modo que los
    umbrales de persistencia calibrados siguen valiendo.

    De paso desaparece el tiempo muerto de arranque: cada apertura
    costaba fijar tasa de muestreo, filtro y sintonia antes de la primera
    muestra util. Ahora ese coste se paga una vez.
    """

    # Si no llega una sola linea en este tiempo, el receptor esta
    # atascado: el proceso sigue vivo pero no entrega nada.
    SILENCIO_MAX_S = 8.0

    def __init__(self, f_min: int, f_max: int, ancho_bin: int):
        self.f_min = f_min
        self.f_max = f_max
        self.ancho_bin = ancho_bin
        self._proc = None
        self._hilo = None
        self._acc = {}
        self._lock = threading.Lock()
        self._ultima_linea = 0.0

    # -- ciclo de vida

    def arrancar(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                ["hackrf_sweep", "-f", f"{self.f_min}:{self.f_max}",
                 "-w", str(self.ancho_bin)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            emit({"ts": now_iso(), "type": "error",
                  "msg": f"no se pudo ejecutar hackrf_sweep: {exc}"})
            self._proc = None
            return False

        with self._lock:
            self._acc = {}
        self._ultima_linea = time.monotonic()
        self._hilo = threading.Thread(target=self._leer, daemon=True)
        self._hilo.start()
        return True

    def parar(self) -> None:
        _cerrar(self._proc)
        if self._hilo is not None:
            self._hilo.join(timeout=3)
        self._proc = None
        self._hilo = None

    def reiniciar(self) -> bool:
        """Ultimo recurso cuando el receptor deja de entregar datos."""
        self.parar()
        time.sleep(1.0)
        return self.arrancar()

    # -- lectura

    def _leer(self) -> None:
        """Hilo lector: vacia la tuberia sin descanso.

        Tiene que consumir de forma continua aunque nadie este pidiendo
        barridos. Si se dejara de leer, el buffer de la tuberia se
        llenaria, hackrf_sweep se bloquearia escribiendo y el receptor
        volveria a quedarse a medias, que es el fallo que esta clase
        existe para evitar.
        """
        try:
            for linea in self._proc.stdout:
                self._ultima_linea = time.monotonic()
                campos = linea.strip().split(", ")
                if len(campos) < 7:
                    continue
                try:
                    f_ini = int(campos[2])
                    paso = float(campos[4])
                    valores = [(int((f_ini + k * paso) / 1e6), float(v))
                               for k, v in enumerate(campos[6:])]
                except (ValueError, IndexError):
                    continue
                with self._lock:
                    for f, db in valores:
                        if f not in self._acc or db > self._acc[f]:
                            self._acc[f] = db
        except (OSError, ValueError):
            pass

    def vivo(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and time.monotonic() - self._ultima_linea < self.SILENCIO_MAX_S)

    def tomar_barrido(self, duracion: float) -> dict:
        """Una ventana de `duracion` segundos: maximo por bin.

        Se vacia el acumulador al entrar, no al salir: asi la ventana
        contiene solo lo observado durante ella y no arrastra energia de
        la ventana anterior, que falsearia la persistencia.
        """
        with self._lock:
            self._acc = {}
        fin = time.monotonic() + duracion
        while _running and time.monotonic() < fin:
            time.sleep(min(0.1, max(0.0, fin - time.monotonic())))
        with self._lock:
            barrido = self._acc
            self._acc = {}
        return barrido


# Receptor compartido por todo el proceso: el HackRF solo admite un
# dueno, y abrirlo mas de una vez es precisamente lo que se quiere
# evitar.
_receptor = None


def receptor(f_min: int, f_max: int, ancho_bin: int) -> "Receptor":
    global _receptor
    if (_receptor is None or _receptor.f_min != f_min
            or _receptor.f_max != f_max or _receptor.ancho_bin != ancho_bin):
        if _receptor is not None:
            _receptor.parar()
        _receptor = Receptor(f_min, f_max, ancho_bin)
    return _receptor


def cerrar_receptor() -> None:
    global _receptor
    if _receptor is not None:
        _receptor.parar()
        _receptor = None


def un_barrido(f_min: int, f_max: int, ancho_bin: int,
               duracion: float) -> dict:
    """Un barrido suelto. Devuelve {frecuencia_mhz: potencia_db_max}."""
    rx = receptor(f_min, f_max, ancho_bin)
    if not rx.arrancar():
        return {}
    return rx.tomar_barrido(duracion)


def capturar(args, progreso: bool = False) -> list:
    """N barridos consecutivos sobre un unico flujo del receptor."""
    rx = receptor(args.f_min, args.f_max, args.ancho_bin)

    if not rx.arrancar():
        return []

    # Un receptor que sigue vivo pero lleva segundos sin entregar una
    # linea esta atascado. Se reintenta una vez antes de darlo por
    # perdido; si tampoco asi responde, se devuelve vacio y el bucle
    # principal lo declara caido en el snapshot y en el diario.
    if not rx.vivo():
        emit({"ts": now_iso(), "type": "error", "mode": "rf",
              "subsistema": "receptor", "estado": "reiniciando",
              "msg": "sin datos del receptor, reabriendo hackrf_sweep"})
        if not rx.reiniciar():
            return []
        time.sleep(1.0)
        if not rx.vivo():
            return []

    barridos = []
    for i in range(args.barridos):
        if not _running:
            break
        if progreso:
            print(f"\r  barrido {i + 1}/{args.barridos}...", end="", flush=True)
        barridos.append(rx.tomar_barrido(args.duracion))
    if progreso:
        print("\r" + " " * 40 + "\r", end="")
    return barridos


# ------------------------------------------------------------------ analisis

def analizar(barridos: list, margen: float) -> dict:
    """Analisis conjunto de energia y persistencia en un solo pase.

    El suelo de ruido se estima como la mediana de las medianas de cada
    barrido individual, no sobre el acumulado. Es la diferencia critica:
    sobre el acumulado, una emision de salto de frecuencia ocupa casi toda
    la banda y desplaza la mediana decenas de dB. En un barrido corto solo
    aparecen los pocos canales que llego a visitar, y la mediana se
    mantiene en el ruido real.
    """
    validos = [b for b in barridos if b]
    n = len(validos)
    if n == 0:
        return {"n_barridos": 0, "suelo_ruido_db": 0.0, "umbral_db": 0.0,
                "persistencia": {}, "picos": {}}

    ocupaciones = {}
    picos = {}
    suelos = []

    for potencias in validos:
        suelo = mediana(list(potencias.values()))
        suelos.append(suelo)
        umbral = suelo + margen
        for f, db in potencias.items():
            if db >= umbral:
                ocupaciones[f] = ocupaciones.get(f, 0) + 1
                if f not in picos or db > picos[f]:
                    picos[f] = db

    persistencia = {f: c / n for f, c in ocupaciones.items()}
    suelo_global = mediana(suelos)

    return {
        "n_barridos": n,
        "suelo_ruido_db": round(suelo_global, 1),
        "umbral_db": round(suelo_global + margen, 1),
        "persistencia": persistencia,
        "picos": picos,
    }


def clasificar_continua(ini: int, fin: int) -> tuple:
    """Clasifica una emision continua por su ancho y frecuencia central.

    Se usa el ancho sobre umbral absoluto, no el ancho a -3 dB. Medido: el
    ancho a -3 dB es relativo al pico y se estrecha cuando la senal se
    refuerza (3-6 MHz para la misma emision segun su carga), mientras que
    el ancho sobre umbral se mantuvo en 11 MHz en todas las medidas.
    """
    ancho = fin - ini + 1
    centro = (ini + fin) / 2

    if ANCHO_WIFI_MIN <= ancho <= ANCHO_WIFI_MAX:
        candidatos = [(abs(centro - f), c) for c, f in CANALES_WIFI.items()
                      if abs(centro - f) <= TOLERANCIA_WIFI_MHZ]
        if candidatos:
            _, canal = min(candidatos)
            return "wifi", f"canal {canal}"
        return "banda_media", "ancho compatible con WiFi, centro no normalizado"

    if ancho >= 25:
        return "banda_ancha", "ocupacion amplia en frecuencia fija"

    return "continua", "emision en frecuencia fija"


def identificar(analisis: dict) -> list:
    """Traduce persistencia y energia a emisiones identificadas."""
    persistencia = analisis["persistencia"]
    picos = analisis["picos"]
    suelo = analisis["suelo_ruido_db"]
    emisiones = []

    # --- emisiones en frecuencia fija
    # Se agrupan por contiguidad antes de clasificar: un bloque de bins
    # adyacentes con persistencia media es una unica emision, no varias de
    # 1 MHz. Se incluyen los bins intermedios porque una emision con
    # trafico irregular no alcanza persistencia alta en todos sus bins.
    fijas = sorted(f for f, p in persistencia.items() if p > UMBRAL_ESPORADICA)

    for ini, fin in agrupar_contiguas(fijas):
        bins = list(range(ini, fin + 1))
        if len(bins) < MIN_BINS_CONTINUA:
            continue
        p_media = sum(persistencia.get(f, 0.0) for f in bins) / len(bins)
        if p_media < UMBRAL_CONTINUA:
            continue

        pico = max(picos.get(f, suelo) for f in bins)
        tipo, detalle = clasificar_continua(ini, fin)
        emisiones.append({
            "clase": "continua",
            "tipo": tipo,
            "detalle": detalle,
            "f_inicio_mhz": ini,
            "f_fin_mhz": fin,
            "f_centro_mhz": (ini + fin) / 2,
            "ancho_mhz": len(bins),
            "persistencia": round(p_media, 2),
            "pico_db": round(pico, 1),
            "snr_db": round(pico - suelo, 1),
        })

    # --- emisiones de salto de frecuencia
    asignados = set()
    for e in emisiones:
        asignados.update(range(e["f_inicio_mhz"], e["f_fin_mhz"] + 1))

    esporadicas = sorted(f for f, p in persistencia.items()
                         if p <= UMBRAL_ESPORADICA and f not in asignados)

    if len(esporadicas) >= MIN_BINS_FHSS:
        dispersion = max(esporadicas) - min(esporadicas)
        pico = max(picos.get(f, suelo) for f in esporadicas)
        p_media = sum(persistencia[f] for f in esporadicas) / len(esporadicas)
        emisiones.append({
            "clase": "salto_frecuencia",
            "tipo": "fhss",
            "detalle": f"{len(esporadicas)} canales sobre {dispersion} MHz",
            "f_inicio_mhz": min(esporadicas),
            "f_fin_mhz": max(esporadicas),
            "f_centro_mhz": (min(esporadicas) + max(esporadicas)) / 2,
            "ancho_mhz": dispersion,
            "canales_visitados": len(esporadicas),
            "persistencia": round(p_media, 2),
            "pico_db": round(pico, 1),
            "snr_db": round(pico - suelo, 1),
        })

    return emisiones


# -------------------------------------------------------------- lista blanca

def cargar_lista_blanca(ruta: Path) -> list:
    if not ruta.is_file():
        return []
    try:
        with open(ruta) as f:
            return json.load(f).get("emisores", [])
    except (OSError, json.JSONDecodeError) as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"lista blanca ilegible: {exc}"})
        return []


def marcar_conocidos(emisiones: list, lista: list) -> None:
    """Anota cada emision con su etiqueta en la lista blanca, si la tiene."""
    for e in emisiones:
        e["conocido"] = False
        e["etiqueta"] = None
        for entrada in lista:
            f_ini = entrada.get("f_inicio_mhz", 0)
            f_fin = entrada.get("f_fin_mhz", 0)
            clase = entrada.get("clase")
            # Una entrada de la lista solo silencia emisiones de su misma
            # clase: un WiFi declarado no debe enmascarar una FHSS que
            # comparta banda, que es justamente lo que interesa detectar.
            if clase and clase != e["clase"]:
                continue
            if e["f_fin_mhz"] >= f_ini and e["f_inicio_mhz"] <= f_fin:
                e["conocido"] = True
                e["etiqueta"] = entrada.get("etiqueta", "sin etiqueta")
                break


# ------------------------------------------------------------------- salidas

def imprimir_cabecera(args, lista: list) -> None:
    print("=" * 74)
    print("  GUARD - detector de actividad radioelectrica")
    print("=" * 74)
    print(f"\n  banda        : {args.f_min}-{args.f_max} MHz "
          f"({args.ancho_bin / 1e6:.0f} MHz por bin)")
    print(f"  muestreo     : {args.barridos} barridos x {args.duracion:.1f} s "
          f"(~{args.barridos * args.duracion:.0f} s)")
    print(f"  lista blanca : {len(lista)} emisor(es) declarado(s)")


def imprimir_mapa(analisis: dict, f_min: int, f_max: int) -> None:
    persistencia = analisis["persistencia"]
    print("\n  Persistencia por frecuencia\n")
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
    print("\n  leyenda:  = continua    - intermedia    . esporadica")


def imprimir_informe(analisis: dict, emisiones: list) -> None:
    print(f"\n  suelo de ruido : {analisis['suelo_ruido_db']} dB")
    print(f"  umbral         : {analisis['umbral_db']} dB")

    if not emisiones:
        print("\n  Sin actividad por encima del umbral.")
        return

    desconocidas = [e for e in emisiones if not e.get("conocido")]
    print(f"\n  {len(emisiones)} emision(es), "
          f"{len(desconocidas)} sin identificar\n")

    for e in emisiones:
        banda = f"{e['f_inicio_mhz']}-{e['f_fin_mhz']} MHz"
        if e["clase"] == "continua":
            titulo = f"CONTINUA   {banda} ({e['ancho_mhz']} MHz)"
        else:
            titulo = f"FHSS       {banda} ({e['canales_visitados']} canales)"

        print(f"  {titulo}")
        print(f"             tipo         : {e['tipo']} - {e['detalle']}")
        print(f"             pico / SNR   : {e['pico_db']} dB / {e['snr_db']} dB")
        print(f"             persistencia : {e['persistencia'] * 100:.0f} %")
        if e.get("conocido"):
            print(f"             estado       : CONOCIDA ({e['etiqueta']})")
        else:
            print("             estado       : >>> NO IDENTIFICADA")
        print()


def emitir_eventos(emisiones: list) -> int:
    """Emite un evento por cada emision no identificada."""
    n = 0
    for e in emisiones:
        if e.get("conocido"):
            continue
        emit({
            "ts": now_iso(),
            "type": "detection",
            "confirmed": True,
            "label": "rf_no_identificada",
            "model": None,
            "confidence": min(0.99, round(e["snr_db"] / 30.0, 2)),
            "window_s": 0.0,
            "rssi_dbfs": e["pico_db"],
            "f_centro_mhz": e["f_centro_mhz"],
            "ancho_mhz": e["ancho_mhz"],
            "clase": e["clase"],
            "tipo": e["tipo"],
            "persistencia": e["persistencia"],
        })
        n += 1
    return n


def escribir_snapshot(args, analisis: dict, emisiones: list,
                      receptor: str = "ok") -> None:
    """Publica el ultimo analisis para otros consumidores del sistema.

    La escritura es atomica —fichero temporal y rename— porque el panel
    puede estar leyendo justo en ese instante y un JSON a medias lo
    dejaria sin datos durante todo un ciclo.

    Se publica tambien cuando no hay datos, con receptor='sin_datos'. Un
    snapshot ausente y un snapshot que declara el receptor caido son dos
    situaciones distintas para el panel: la primera solo dice que el
    detector no esta publicando —puede estar parado, arrancando o
    colgado—, la segunda afirma que el detector vive y que lo que falla
    es el receptor. Distinguirlas es la diferencia entre un operador que
    sabe que no esta vigilando y uno que cree que si.
    """
    try:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({
                "ts": now_iso(),
                "ts_mono": round(time.monotonic(), 1),
                "receptor": receptor,
                "banda_mhz": [args.f_min, args.f_max],
                "barridos": analisis["n_barridos"],
                "duracion_barrido_s": args.duracion,
                "margen_db": args.margen,
                "suelo_ruido_db": analisis["suelo_ruido_db"],
                "umbral_db": analisis["umbral_db"],
                "emisiones": emisiones,
                "persistencia_por_bin": {
                    str(f): round(p, 3)
                    for f, p in sorted(analisis["persistencia"].items())
                },
                "picos_por_bin": {
                    str(f): round(v, 1)
                    for f, v in sorted(analisis["picos"].items())
                },
            }, f, separators=(",", ":"))
        os.replace(tmp, SNAPSHOT_FILE)
    except OSError as exc:
        emit({"ts": now_iso(), "type": "error",
              "msg": f"no se pudo publicar el snapshot: {exc}"})


def volcar_json(destino: Path, args, analisis: dict, emisiones: list) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w") as f:
        json.dump({
            "ts": now_iso(),
            "banda_mhz": [args.f_min, args.f_max],
            "barridos": analisis["n_barridos"],
            "duracion_barrido_s": args.duracion,
            "margen_db": args.margen,
            "suelo_ruido_db": analisis["suelo_ruido_db"],
            "umbral_db": analisis["umbral_db"],
            "emisiones": emisiones,
            "persistencia_por_bin": {
                str(f): round(p, 3)
                for f, p in sorted(analisis["persistencia"].items())
            },
        }, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------- modos

def modo_informe(args) -> int:
    lista = cargar_lista_blanca(args.config)
    if not args.json:
        imprimir_cabecera(args, lista)

    barridos = capturar(args, progreso=not args.json)
    if not any(barridos):
        print("ERROR: ningun barrido devolvio datos.", file=sys.stderr)
        return 1

    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, lista)

    if args.json:
        print(json.dumps({
            "ts": now_iso(),
            "banda_mhz": [args.f_min, args.f_max],
            "suelo_ruido_db": analisis["suelo_ruido_db"],
            "emisiones": emisiones,
        }, indent=2, ensure_ascii=False))
    else:
        if args.mapa:
            imprimir_mapa(analisis, args.f_min, args.f_max)
        imprimir_informe(analisis, emisiones)

    if args.guardar:
        volcar_json(args.guardar, args, analisis, emisiones)
        if not args.json:
            print(f"  resultado en {args.guardar}")

    return 0


def modo_calibrar(args) -> int:
    print("=" * 74)
    print("  GUARD - calibracion del entorno radioelectrico")
    print("=" * 74)
    print(f"\n  Caracterizando {args.f_min}-{args.f_max} MHz...")
    print("  (asegurate de que NO hay emisores de interes activos)\n")

    barridos = capturar(args, progreso=True)
    if not any(barridos):
        print("ERROR: ningun barrido devolvio datos.", file=sys.stderr)
        return 1

    analisis = analizar(barridos, args.margen)
    emisiones = identificar(analisis)
    marcar_conocidos(emisiones, [])
    imprimir_informe(analisis, emisiones)

    propuesta = {
        "generada": now_iso(),
        "nota": ("Revisar antes de usar. Cada entrada silencia esa banda "
                 "para emisiones de la misma clase."),
        "emisores": [
            {
                "etiqueta": f"{e['tipo']} {e['detalle']}".strip(),
                "clase": e["clase"],
                "f_inicio_mhz": e["f_inicio_mhz"],
                "f_fin_mhz": e["f_fin_mhz"],
            }
            for e in emisiones
        ],
    }

    args.config.parent.mkdir(parents=True, exist_ok=True)
    destino = args.config.with_suffix(".propuesta.json")
    with open(destino, "w") as f:
        json.dump(propuesta, f, indent=2, ensure_ascii=False)

    print(f"  Propuesta de lista blanca en:\n    {destino}\n")
    print(f"  Revisala y, si procede:\n    mv {destino} {args.config}")
    return 0


def modo_continuo(args) -> int:
    lista = cargar_lista_blanca(args.config)
    emit({"ts": now_iso(), "type": "status", "mode": "rf",
          "pid": os.getpid(), "banda_mhz": [args.f_min, args.f_max],
          "lista_blanca": len(lista)})

    ultimo_status = time.monotonic()
    ciclos_sin_datos = 0

    while _running:
        touch_health()
        barridos = capturar(args)

        if not any(barridos):
            # El receptor no entrega datos. Puede ser el USB atascado, el
            # dispositivo retirado o hackrf_sweep fallando al abrirlo. No
            # se puede distinguir desde aqui, pero si se puede —y se
            # debe— decir que no se esta vigilando.
            ciclos_sin_datos += 1
            escribir_snapshot(args, analizar([], args.margen), [],
                              receptor="sin_datos")
            # Se avisa al entrar en el fallo y luego con cuentagotas: un
            # receptor caido no debe inundar el diario, pero tampoco
            # puede desaparecer de el mientras dure.
            if ciclos_sin_datos == 1 or ciclos_sin_datos % 20 == 0:
                emit({"ts": now_iso(), "type": "error", "mode": "rf",
                      "subsistema": "receptor", "estado": "sin_datos",
                      "ciclos": ciclos_sin_datos,
                      "msg": "el receptor no entrega barridos"})
            if _running:
                time.sleep(max(0.0, args.intervalo))
            continue

        if ciclos_sin_datos:
            emit({"ts": now_iso(), "type": "status", "mode": "rf",
                  "subsistema": "receptor", "estado": "recuperado",
                  "ciclos_perdidos": ciclos_sin_datos})
            ciclos_sin_datos = 0

        analisis = analizar(barridos, args.margen)
        emisiones = identificar(analisis)
        marcar_conocidos(emisiones, lista)

        # Dos consumidores, un solo analisis: los eventos van al puente y
        # de ahi a la interfaz fisica; el snapshot lo lee el panel. Ambos
        # ven exactamente lo mismo.
        emitir_eventos(emisiones)
        escribir_snapshot(args, analisis, emisiones)

        if time.monotonic() - ultimo_status >= 30:
            emit({"ts": now_iso(), "type": "status", "mode": "rf",
                  "suelo_ruido_db": analisis["suelo_ruido_db"],
                  "emisiones": len(emisiones),
                  "no_identificadas": sum(1 for e in emisiones
                                          if not e.get("conocido"))})
            ultimo_status = time.monotonic()

        if _running:
            time.sleep(max(0.0, args.intervalo))

    emit({"ts": now_iso(), "type": "status", "mode": "rf", "stopping": True})
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Detector de actividad radioelectrica no identificada")
    modo = p.add_mutually_exclusive_group()
    modo.add_argument("--continuo", action="store_true",
                      help="servicio: analiza en bucle y emite eventos")
    modo.add_argument("--calibrar", action="store_true",
                      help="caracteriza el entorno y propone lista blanca")

    p.add_argument("--f-min", type=int, default=BANDA_MIN_MHZ)
    p.add_argument("--f-max", type=int, default=BANDA_MAX_MHZ)
    p.add_argument("--ancho-bin", type=int, default=ANCHO_BIN_HZ)
    p.add_argument("--barridos", type=int, default=N_BARRIDOS,
                   help=f"barridos por analisis (def. {N_BARRIDOS})")
    p.add_argument("--duracion", type=float, default=DURACION_BARRIDO_S,
                   help="segundos por barrido individual")
    p.add_argument("--margen", type=float, default=MARGEN_DB,
                   help="dB sobre el suelo de ruido para considerar ocupacion")
    p.add_argument("--intervalo", type=float, default=2.0,
                   help="pausa entre analisis en modo continuo")
    p.add_argument("--mapa", action="store_true",
                   help="muestra la persistencia bin a bin")
    p.add_argument("--json", action="store_true", help="salida estructurada")
    p.add_argument("--guardar", type=Path, default=None,
                   help="escribe el analisis completo a un fichero JSON")
    p.add_argument("--config", type=Path, default=CONFIG_DEF)
    args = p.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # El receptor se cierra pase lo que pase: un hackrf_sweep huerfano
    # dejaria el HackRF ocupado y el siguiente arranque del servicio no
    # podria abrirlo.
    try:
        if args.calibrar:
            return modo_calibrar(args)
        if args.continuo:
            return modo_continuo(args)
        return modo_informe(args)
    finally:
        cerrar_receptor()


if __name__ == "__main__":
    sys.exit(main())
