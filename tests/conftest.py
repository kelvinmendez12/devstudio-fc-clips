"""Marcador sintético: plantillas y fotogramas generados con las mismas glifos de OpenCV."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rkm_clips.detector import Templates, imwrite  # noqa: E402

FONT = cv2.FONT_HERSHEY_SIMPLEX
SCALE = 1.0
THICK = 2
BG = 40
FG = 235


def render(text: str) -> np.ndarray:
    """Texto claro sobre fondo oscuro, recortado al contenido con 2 px de margen."""
    canvas = np.full((80, 40 * len(text) + 40), BG, np.uint8)
    cv2.putText(canvas, text, (10, 55), FONT, SCALE, FG, THICK, cv2.LINE_AA)
    ys, xs = np.nonzero(canvas > BG + 30)
    return canvas[ys.min() - 2: ys.max() + 3, xs.min() - 2: xs.max() + 3].copy()


@pytest.fixture(scope="session")
def templates() -> Templates:
    return Templates(label=render("RKM"), digits={d: render(str(d)) for d in range(10)})


@pytest.fixture
def template_dir(tmp_path, templates) -> Path:
    imwrite(tmp_path / "rkm_label.png", templates.label)
    for d, img in templates.digits.items():
        imwrite(tmp_path / "digits" / f"{d}.png", img)
    return tmp_path


def paste(canvas: np.ndarray, img: np.ndarray, x: int, y: int) -> int:
    h, w = img.shape
    canvas[y:y + h, x:x + w] = img
    return x + w


def scoreboard(value: int | None, templates: Templates, size=(240, 360), noise: float = 0,
               extra_right: str | None = None) -> np.ndarray:
    """ROI con una fila "KDA 7" encima y la fila "RKM <value>"; value=None oculta el marcador."""
    h, w = size
    roi = np.full((h, w), BG, np.uint8)
    if value is not None:
        x = paste(roi, render("KDA"), 20, 40)
        paste(roi, templates.digits[7], x + 18, 40)
        x = paste(roi, templates.label, 20, 110) + 18
        for ch in str(value):
            x = paste(roi, templates.digits[int(ch)], x, 110) + 3
        if extra_right:
            for ch in extra_right:
                x = paste(roi, templates.digits[int(ch)], x + 60, 110)
    if noise:
        rng = np.random.default_rng(value or 0)
        roi = np.clip(roi + rng.normal(0, noise, roi.shape), 0, 255).astype(np.uint8)
    return roi
