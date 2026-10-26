"""Carga de fuentes del dibujo del vídeo: las incluidas en la app, con respaldo del sistema.

Se leen a bytes y se abren desde memoria: así no importa que la ruta del usuario tenga acentos («Méndez») ni
que Windows deje el archivo bloqueado. FreeType no es seguro entre hilos, por eso todo el dibujo del módulo
`scoreboard_art` se hace con `LOCK` cogido.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
LOCK = threading.RLock()

# rol -> archivos en orden de preferencia (los de la app primero, luego el sistema)
ROLES: dict[str, list[str]] = {
    "number": ["BarlowCondensed-ExtraBoldItalic.ttf", r"C:\Windows\Fonts\impact.ttf", "DejaVuSans-Bold.ttf"],
    "headline": ["BarlowCondensed-BlackItalic.ttf", r"C:\Windows\Fonts\impact.ttf", "DejaVuSans-Bold.ttf"],
    "text": ["BarlowCondensed-ExtraBold.ttf", r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\arialbd.ttf",
             "DejaVuSans-Bold.ttf"],
}

_bytes: dict[str, bytes | None] = {}
_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _read(name: str) -> bytes | None:
    if name not in _bytes:
        path = FONT_DIR / name if not Path(name).is_absolute() and (FONT_DIR / name).exists() else Path(name)
        try:
            _bytes[name] = path.read_bytes()
        except OSError:
            _bytes[name] = None
    return _bytes[name]


def get_font(role: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Fuente del rol a ese tamaño; nunca falla (en el peor caso, la de Pillow)."""
    key = (role, size)
    with LOCK:
        if key in _cache:
            return _cache[key]
        for candidate in ROLES[role]:
            data = _read(candidate)
            if data is None:
                continue
            try:
                font = ImageFont.truetype(io.BytesIO(data), size)
            except OSError:
                continue
            _cache[key] = font
            return font
        log.warning("Sin fuentes para «%s»: se usa la de Pillow", role)
        _cache[key] = ImageFont.load_default(size)
        return _cache[key]


def clear_cache() -> None:
    with LOCK:
        _bytes.clear()
        _cache.clear()
