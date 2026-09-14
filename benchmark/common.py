"""
Utilidades compartidas para entrenamiento y evaluacion del detector de drones.
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

CLASS_TO_IDX: dict[str, int] = {"interference": 0, "drone": 1}
IDX_TO_CLASS: dict[int, str] = {v: k for k, v in CLASS_TO_IDX.items()}

_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD  = [0.229, 0.224, 0.225]


def set_seed(seed: int = 42) -> None:
    """Fija todas las semillas para reproducibilidad completa."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


class PowerAblationNormalize:
    """
    Destruye el nivel medio absoluto del espectrograma y preserva 
    solo la morfología relativa de los bursts. Si el modelo sigue 
    funcionando bien tras esto, su discriminacion se basa en forma, 
    no en intensidad.
    """
    def __call__(self, img):
        arr = np.array(img, dtype=np.float32)
        # Estandarizar por imagen (media 0, std 1 globales)
        arr = (arr - arr.mean()) / (arr.std() + 1e-8)
        # Re-escalar a 0-255
        arr = (arr - arr.min()) / (arr.max() - arr.min() + 1e-8) * 255
        return Image.fromarray(arr.astype(np.uint8))
    



    


class SpectrogramCSVDataset(Dataset):
    """
    Dataset de espectrogramas indexado por CSV.

    El CSV debe tener al menos 'filename' y 'label'.
    'filename' es relativo a root_dir (ej. 'all/drone/seg0000.png').
    """

    def __init__(
        self,
        csv_path: str | Path,
        root_dir: str | Path,
        transform=None,
    ) -> None:
        self.df = pd.read_csv(csv_path)
        self.root_dir = Path(root_dir)
        self.transform = transform

        missing = {"filename", "label"} - set(self.df.columns)
        if missing:
            raise ValueError(f"Faltan columnas en {csv_path}: {missing}")

        unknown = set(self.df["label"].unique()) - set(CLASS_TO_IDX)
        if unknown:
            raise ValueError(f"Etiquetas desconocidas: {unknown}")

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, idx: int) -> tuple:
        row = self.df.iloc[idx]
        img_path = self.root_dir / row["filename"]
        label = CLASS_TO_IDX[row["label"]]
        image = Image.open(img_path).convert("RGB")
        if self.transform is not None:
            image = self.transform(image)
        return image, label


class FrequencyNormalize:
    """
    Centra el espectrograma en la banda de maxima energia.

    Convierte el eje Y de frecuencia absoluta a frecuencia relativa al pico,
    haciendo al modelo invariante a la Fc de captura. Util cuando el dron puede
    aparecer a distinta frecuencia segun el barrido o el hopping.

    Pasos:
      1. Calcula el perfil de energia por bin de frecuencia (media temporal en escala
         de grises, independiente del colormap).
      2. Localiza el bin de maxima energia (fila de mayor brillo medio).
      3. Recorta una ventana de window_frac * altura centrada en ese bin.

    El recorte se pasa luego a Resize(224, 224), que absorbe el cambio de aspecto.
    """

    def __init__(self, window_frac: float = 0.5) -> None:
        if not 0 < window_frac <= 1.0:
            raise ValueError("window_frac debe estar en (0, 1]")
        self.window_frac = window_frac

    def __call__(self, img: Image.Image) -> Image.Image:
        gray = np.array(img.convert("L"), dtype=np.float32)  # (H, W)
        freq_profile = gray.mean(axis=1)                     # media por fila (frecuencia)
        peak_row = int(np.argmax(freq_profile))

        window = max(1, int(img.height * self.window_frac))
        top = peak_row - window // 2
        top = max(0, min(top, img.height - window))
        bottom = top + window

        return img.crop((0, top, img.width, bottom))


class PowerAblationNormalize:
    """
    Destruye el nivel absoluto de potencia del espectrograma preservando
    solo la morfologia relativa de los bursts.

    Procedimiento:
      1. Convierte la imagen a escala de grises (extrae potencia, descartando colormap).
      2. Estandariza por imagen: cada espectrograma queda con media 0 y std 1.
      3. Recorta valores extremos a +/- clip_sigma para evitar que outliers dominen.
      4. Re-escala linealmente al rango 0-255 y vuelve a 3 canales RGB para la red.

    Tras esta transformacion, dos espectrogramas con la misma forma pero distinta
    intensidad media se vuelven indistinguibles. Si el modelo entrenado con esta
    transformacion mantiene F1 alto, su discriminacion se basa en la *forma* de los
    bursts (morfologia FHSS) y no en la *intensidad* absoluta de la senal.

    Es la transformacion recomendada para experimentos de ablacion: comparando F1
    con y sin PowerAblationNormalize se cuantifica cuanto del rendimiento del
    modelo proviene del nivel de senal vs. la morfologia espectral.
    """

    def __init__(self, clip_sigma: float = 3.0) -> None:
        if clip_sigma <= 0:
            raise ValueError("clip_sigma debe ser > 0")
        self.clip_sigma = float(clip_sigma)

    def __call__(self, img: Image.Image) -> Image.Image:
        # 1. Escala de grises -> array float
        gray = np.array(img.convert("L"), dtype=np.float32)  # (H, W)

        # 2. Estandarizar por imagen (media 0, std 1)
        mu = gray.mean()
        sigma = gray.std()
        if sigma < 1e-8:
            # Imagen casi uniforme: devolver gris medio replicado para no romper la red
            flat = np.full_like(gray, 127.0, dtype=np.uint8)
            return Image.fromarray(flat).convert("RGB")
        gray = (gray - mu) / sigma

        # 3. Clip a +/- clip_sigma desviaciones para limitar outliers
        gray = np.clip(gray, -self.clip_sigma, self.clip_sigma)

        # 4. Re-escalar a 0-255 lineal y a RGB de 3 canales identicos
        gray = (gray + self.clip_sigma) / (2.0 * self.clip_sigma) * 255.0
        gray = gray.astype(np.uint8)
        return Image.fromarray(gray).convert("RGB")


def build_transforms(train: bool, freq_normalize: bool = True, power_ablation: bool = False) -> transforms.Compose:
    """
    Transformaciones para espectrogramas RF.

    Si freq_normalize=True, antepone FrequencyNormalize para que la red aprenda
    la huella temporal del dron y no la frecuencia absoluta.

    Si power_ablation=True, anade PowerAblationNormalize despues de
    FrequencyNormalize para destruir el nivel absoluto de potencia y forzar
    que la red aprenda solo morfologia. Util para experimentos de ablacion.

    El orden es importante: FrequencyNormalize NECESITA el perfil de energia
    para localizar el pico, asi que tiene que ir ANTES de la ablacion de potencia.

    Sin flips ni rotaciones: el eje vertical es frecuencia y el color codifica
    intensidad; invertirlos cambia el significado fisico de la imagen.
    """
    normalize = transforms.Normalize(mean=_IMAGENET_MEAN, std=_IMAGENET_STD)

    steps = []
    if freq_normalize:
        steps.append(FrequencyNormalize(window_frac=0.5))

    if power_ablation:
        steps.append(PowerAblationNormalize(clip_sigma=3.0))

    steps.append(transforms.Resize((224, 224)))

    if train:
        steps += [
            transforms.RandomAffine(
                degrees=0,
                translate=(0.02, 0.02),
                scale=(0.95, 1.05),
            ),
            transforms.RandomApply([transforms.GaussianBlur(kernel_size=3)], p=0.2),
        ]

    steps += [transforms.ToTensor(), normalize]
    return transforms.Compose(steps)


def _worker_init_fn(worker_id: int) -> None:
    np.random.seed(42 + worker_id)


def make_loader(
    csv_path: str | Path,
    root_dir: str | Path,
    train: bool,
    batch_size: int = 32,
    num_workers: int = 0,
    freq_normalize: bool = True,
    power_ablation: bool = False,
) -> DataLoader:
    """
    Construye un DataLoader reproducible para el CSV dado.

    power_ablation=True activa PowerAblationNormalize tras FrequencyNormalize
    para experimentos que desacoplan morfologia de intensidad.
    """
    dataset = SpectrogramCSVDataset(
        csv_path=csv_path,
        root_dir=root_dir,
        transform=build_transforms(
            train,
            freq_normalize=freq_normalize,
            power_ablation=power_ablation,
        ),
    )
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=train,
        num_workers=num_workers,
        pin_memory=torch.cuda.is_available(),
        worker_init_fn=_worker_init_fn if num_workers > 0 else None,
        generator=torch.Generator().manual_seed(42),
    )
