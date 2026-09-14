"""
spectrogram_render.py
=====================

Render in-memory de espectrogramas a partir de IQ, replicando 1:1 los
parametros de make_spectrogram_dataset.py para que las imagenes pasadas al
clasificador sean estadisticamente equivalentes a las usadas en
entrenamiento.

Por que existe este modulo:
    El barrido de SNR necesita regenerar miles de espectrogramas con ruido
    inyectado. Escribir cada uno a disco como PNG seria costoso y, ademas,
    queremos que pasen por las mismas transforms del modelo (FrequencyNormalize,
    Resize, ImageNet norm). Aqui devolvemos PIL.Image en RAM.

Parametros copiados de scripts/make_spectrogram_dataset.py:
    NFFT = 1024
    win = hann(NFFT, sym=False)
    overlap = round(0.75 * NFFT)
    detrend=False, return_onesided=False, scaling='density', mode='complex'
    Pdb = 10*log10(|S|^2 + eps), fftshift
    modo_escala = "auto" -> clim = (maxDb - 80, maxDb)
    img_width_px = 1024, img_height_px = 576, dpi = 120
    colormap = parula aproximada
    pad_inches=0 al guardar.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.signal import spectrogram, windows

# matplotlib se importa de forma diferida (solo en el render lento) para
# evitar pagar el coste de import si se usa unicamente el render rapido.


# ---------------------------------------------------------------------------
# Parametros fijos (deben coincidir con make_spectrogram_dataset.py)
# ---------------------------------------------------------------------------

NFFT = 1024
OVERLAP = int(round(0.75 * NFFT))
WIN = windows.hann(NFFT, sym=False)

IMG_WIDTH_PX = 1024
IMG_HEIGHT_PX = 576
EXPORT_DPI = 120
RANGO_DINAMICO_DB = 80


# ---------------------------------------------------------------------------
# Colormap parula aproximada (copiado tal cual del script de generacion)
# ---------------------------------------------------------------------------

_PARULA_COLORS = [
    (0.2422, 0.1504, 0.6603),
    (0.2504, 0.1650, 0.7076),
    (0.2578, 0.1818, 0.7511),
    (0.2647, 0.1978, 0.7952),
    (0.2706, 0.2147, 0.8364),
    (0.2751, 0.2342, 0.8710),
    (0.2783, 0.2559, 0.8991),
    (0.2803, 0.2782, 0.9221),
    (0.2813, 0.3006, 0.9414),
    (0.2809, 0.3228, 0.9579),
    (0.2784, 0.3447, 0.9717),
    (0.2699, 0.4000, 0.9892),
    (0.2394, 0.5177, 0.9692),
    (0.1890, 0.6200, 0.9180),
    (0.1570, 0.7000, 0.8310),
    (0.1960, 0.7600, 0.6870),
    (0.3650, 0.8000, 0.4940),
    (0.6000, 0.8400, 0.2720),
    (0.8200, 0.8900, 0.1400),
    (0.9769, 0.9839, 0.0805),
]
def _build_parula_lut(n: int = 256) -> np.ndarray:
    """
    Construye una LUT (n, 3) uint8 interpolando linealmente entre los 20
    puntos de control del parula. Equivalente practico a
    LinearSegmentedColormap.from_list("parula", _PARULA_COLORS, N=n) sin
    necesidad de importar matplotlib.
    """
    pts = np.array(_PARULA_COLORS, dtype=np.float32)  # (20, 3)
    x_old = np.linspace(0.0, 1.0, pts.shape[0])
    x_new = np.linspace(0.0, 1.0, n)
    lut = np.zeros((n, 3), dtype=np.float32)
    for ch in range(3):
        lut[:, ch] = np.interp(x_new, x_old, pts[:, ch])
    return np.clip(lut * 255.0, 0, 255).astype(np.uint8)


_PARULA_LUT = _build_parula_lut(256)


# ---------------------------------------------------------------------------
# Lectura de segmento IQ
# ---------------------------------------------------------------------------

def read_iq_segment(
    sigmf_data_path: Path,
    start_sample: int,
    num_samples: int,
) -> np.ndarray | None:
    """
    Lee un segmento de un .sigmf-data en formato cf32_le interleaved
    (I, Q, I, Q, ... float32 little endian). Devuelve complex64.

    Aplica DC removal (resta de la media), igual que en
    make_spectrogram_dataset.py.
    """
    offset_bytes = start_sample * 2 * 4
    count_float32 = num_samples * 2

    raw = np.fromfile(
        sigmf_data_path,
        dtype=np.float32,
        count=count_float32,
        offset=offset_bytes,
    )
    if raw.size < count_float32:
        return None

    i = raw[0::2]
    q = raw[1::2]
    x = (i.astype(np.float32) + 1j * q.astype(np.float32)).astype(np.complex64)
    x = x - np.mean(x)
    return x


# ---------------------------------------------------------------------------
# STFT y render
# ---------------------------------------------------------------------------

def iq_to_pdb(iq: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Calcula la matriz P_dB del espectrograma con los mismos parametros que
    make_spectrogram_dataset.py.

    Devuelve:
        F: vector de frecuencias en Hz centrado (fftshift aplicado).
        T: vector temporal (s).
        Pdb: matriz de potencia en dB.
    """
    F, T, S = spectrogram(
        iq,
        fs=fs,
        window=WIN,
        nperseg=NFFT,
        noverlap=OVERLAP,
        nfft=NFFT,
        detrend=False,
        return_onesided=False,
        scaling="density",
        mode="complex",
    )
    F = np.fft.fftshift(F)
    S = np.fft.fftshift(S, axes=0)
    Pdb = 10.0 * np.log10(np.abs(S) ** 2 + np.finfo(float).eps)
    return F, T, Pdb


def render_spectrogram_image(
    iq: np.ndarray,
    fs: float,
    fc: float,
) -> Image.Image:
    """
    Render rapido: aplica LUT parula sobre la matriz Pdb normalizada y
    redimensiona a 1024x576 con PIL. Sin matplotlib.

    Imita 1:1 lo que hace make_spectrogram_dataset.py:
      - Pdb = 10*log10(|S|^2 + eps) con la misma STFT (Hann 1024, overlap 0.75).
      - clim auto: (maxDb - 80, maxDb).
      - origin="lower": frecuencia baja abajo -> flipud sobre Pdb antes de PIL.
      - interpolation="nearest" al redimensionar.
      - Aspect "auto" con extent que llena toda la figura -> equivalente a
        un resize directo a (1024, 576) sin preservar aspect ratio.

    Devuelve PIL.Image RGB de tamano (IMG_WIDTH_PX, IMG_HEIGHT_PX).
    """
    _F, _T, Pdb = iq_to_pdb(iq, fs=fs)

    maxDb = float(np.max(Pdb))
    clim_lo = maxDb - RANGO_DINAMICO_DB
    # Normalizar a [0, 1] respetando el clim
    pdb_norm = np.clip((Pdb - clim_lo) / float(RANGO_DINAMICO_DB), 0.0, 1.0)
    # Indices en la LUT
    idx = np.clip(
        np.round(pdb_norm * (_PARULA_LUT.shape[0] - 1)).astype(np.int32),
        0, _PARULA_LUT.shape[0] - 1,
    )
    rgb = _PARULA_LUT[idx]  # (Nfreq, Ntime, 3) uint8

    # origin="lower" -> volcar verticalmente para que la fila 0 quede abajo
    rgb = np.ascontiguousarray(rgb[::-1, :, :])

    img = Image.fromarray(rgb, mode="RGB")
    img = img.resize((IMG_WIDTH_PX, IMG_HEIGHT_PX), Image.NEAREST)
    return img


def render_spectrogram_image_matplotlib(
    iq: np.ndarray,
    fs: float,
    fc: float,
) -> Image.Image:
    """
    Render lento de respaldo via matplotlib + savefig. Pensado para
    validar la version rapida y para casos donde se quiera un PNG
    pixel-perfect identico a los de entrenamiento.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    parula_cmap = LinearSegmentedColormap.from_list("parula_custom", _PARULA_COLORS, N=256)

    F, T, Pdb = iq_to_pdb(iq, fs=fs)
    F_abs = (F + fc) / 1e6

    fig_w = IMG_WIDTH_PX / EXPORT_DPI
    fig_h = IMG_HEIGHT_PX / EXPORT_DPI
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=EXPORT_DPI)
    ax = fig.add_axes([0, 0, 1, 1])

    im = ax.imshow(
        Pdb,
        aspect="auto",
        origin="lower",
        extent=[
            T[0] if len(T) > 0 else 0.0,
            T[-1] if len(T) > 0 else (iq.shape[0] / fs),
            F_abs[0],
            F_abs[-1],
        ],
        cmap=parula_cmap,
        interpolation="nearest",
    )
    ax.set_ylim((fc - fs / 2) / 1e6, (fc + fs / 2) / 1e6)
    ax.axis("off")

    maxDb = float(np.max(Pdb))
    im.set_clim(maxDb - RANGO_DINAMICO_DB, maxDb)

    buf = io.BytesIO()
    fig.savefig(buf, dpi=EXPORT_DPI, pad_inches=0, format="png")
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    img.load()
    return img


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import time

    sigmf_meta = Path("data_raw/sigmf/hunter/hunter_2.sigmf-meta")
    sigmf_data = Path("data_raw/sigmf/hunter/hunter_2.sigmf-data")
    with open(sigmf_meta, "r", encoding="utf-8") as f:
        meta = json.load(f)
    fs = float(meta["global"]["core:sample_rate"])
    fc = float(meta["captures"][0]["core:frequency"])
    print(f"fs={fs/1e6:.2f} MHz, fc={fc/1e6:.2f} MHz")

    n_samples = int(round(0.1 * fs))
    iq = read_iq_segment(sigmf_data, start_sample=0, num_samples=n_samples)
    print(f"IQ shape: {iq.shape}, dtype: {iq.dtype}")

    # Comparativa de tiempos: matplotlib vs numpy
    t0 = time.time()
    img_fast = render_spectrogram_image(iq, fs=fs, fc=fc)
    t1 = time.time()
    img_mpl = render_spectrogram_image_matplotlib(iq, fs=fs, fc=fc)
    t2 = time.time()

    print(f"Render rapido (numpy):    {1000*(t1-t0):7.1f} ms, size={img_fast.size}")
    print(f"Render lento (matplotlib): {1000*(t2-t1):7.1f} ms, size={img_mpl.size}")

    img_fast.save("/tmp/test_render_fast.png")
    img_mpl.save("/tmp/test_render_mpl.png")
    print("Guardados /tmp/test_render_fast.png y /tmp/test_render_mpl.png")
