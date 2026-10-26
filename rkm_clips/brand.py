"""Identidad del club: logo, color principal y paleta derivada.

El logo se normaliza una sola vez, al subirlo, y se guarda como PNG en `data_dir/club/logo.png`. Los ajustes
(nombre, color, si se muestra el nombre junto al logo) viven en la clave `club_brand` de la tabla `meta`.
El dibujo (`scoreboard_art`) recibe un `Brand` ya calculado y no vuelve a analizar nada.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

META_KEY = "club_brand"
DEFAULT_PRIMARY = "#9146FF"     # el morado de siempre: quien ya usa la app conserva su aspecto
STYLES = ("retransmision",)
DEFAULT_STYLE = STYLES[0]
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 64, 4096
LOGO_MAX = 512
SHORT_NAME_MAX = 14
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class BrandError(Exception):
    """Algo que el usuario puede corregir (logo no válido, color mal escrito…). El texto va al panel."""


# --- color --------------------------------------------------------------------------------------

def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(round(c)) for c in rgb[:3])


def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(a, b) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def _hls(rgb):
    return colorsys.rgb_to_hls(*[c / 255 for c in rgb[:3]])


def _from_hls(h, l, s):
    return tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb(h, max(0, min(1, l)), max(0, min(1, s))))


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class Palette:
    """Toda la paleta sale de UN color. Los neutros llevan el matiz del club (nunca gris puro)."""

    def __init__(self, main_hex: str):
        main = hex_rgb(main_hex)
        h, l, s = _hls(main)
        self.main = main
        ps = min(s, 0.45)
        self.plate_hi = _from_hls(h, 0.15, ps)
        self.plate = _from_hls(h, 0.095, ps)
        self.plate_lo = _from_hls(h, 0.055, ps)
        # acento: el color del club, aclarado solo si no se ve sobre la placa (clubes negros o azul marino)
        accent, al = main, l
        while contrast_ratio(accent, self.plate) < 4.2 and al < 0.9:
            al += 0.02
            accent = _from_hls(h, al, s)
        self.accent = accent
        ah, al_, as_ = _hls(accent)
        self.light = _from_hls(ah, min(al_ + 0.16, 0.93), as_ * 0.95)
        self.dark = _from_hls(ah, al_ * 0.58, min(1, as_ * 1.05))
        self.glow = _from_hls(ah, min(max(al_, 0.5), 0.72), min(1, as_ * 1.1))
        # tinta del texto SOBRE el acento: blanco si contrasta lo bastante; si no, casi negro con matiz
        self.ink_dark = _from_hls(h, 0.07, ps)
        self.ink = (255, 255, 255) if contrast_ratio((255, 255, 255), accent) >= 3.6 else self.ink_dark

    @property
    def accent_ffmpeg(self) -> str:
        """El acento como lo escribe ffmpeg: 0xRRGGBB."""
        return "0x%02X%02X%02X" % self.accent


def extract_colors(logo: Image.Image, limit: int = 3) -> list[str]:
    """Colores vistosos del logo (el primero es el principal). Lista vacía si no tiene ninguno útil."""
    small = logo.convert("RGBA")
    small.thumbnail((64, 64), Image.BOX)
    rgba = np.asarray(small).astype(np.float32)
    hsv = np.asarray(small.convert("RGB").convert("HSV")).astype(np.float32) / 255.0
    alpha = rgba[..., 3] / 255.0
    s, v = hsv[..., 1], hsv[..., 2]
    useful = (alpha >= 0.5) & (s >= 0.25) & (v >= 0.20) & ~((v > 0.95) & (s < 0.35))
    if useful.mean() < 0.02 or not useful.any():
        return []
    hue = hsv[..., 0]
    weight = np.where(useful, s * v * alpha, 0.0)
    bins = np.minimum((hue * 36).astype(int), 35)
    hist = np.bincount(bins.ravel(), weights=weight.ravel(), minlength=36)
    smooth = hist + np.roll(hist, 1) + np.roll(hist, -1)
    found: list[tuple[float, str]] = []
    taken = np.zeros(36, bool)
    for _ in range(limit):
        masked = np.where(taken, -1, smooth)
        peak = int(masked.argmax())
        if masked[peak] <= 0 or (found and masked[peak] < 0.15 * found[0][0]):
            break
        near = [(peak + d) % 36 for d in (-1, 0, 1)]
        sel = useful & np.isin(bins, near)
        # los logos llevan sombras y biseles más oscuros: el color «de verdad» es el de la mitad más clara
        sel &= v >= np.median(v[sel])
        w = weight[sel]
        rgb = rgba[..., :3][sel]
        mean = tuple((rgb * w[:, None]).sum(axis=0) / w.sum())
        h, l, sat = _hls(mean)
        found.append((float(masked[peak]), rgb_hex(_from_hls(h, min(max(l, 0.38), 0.62), max(sat, 0.55)))))
        for d in range(-4, 5):   # el siguiente color debe ser de otro tono (≥ 40°)
            taken[(peak + d) % 36] = True
    return [c for _, c in found]


# --- logo ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LogoResult:
    png: bytes
    width: int
    height: int
    clean: bool            # transparente (o con el fondo liso ya quitado): puede ir sin sello
    colors: list[str] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.png).hexdigest()[:12]


def _flat_background_to_alpha(im: Image.Image, tol: int = 28) -> Image.Image | None:
    """Quita un fondo liso (las 4 esquinas casi iguales) por inundación desde los bordes."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if not all(max(abs(a - b) for a, b in zip(corners[0], c)) <= tol for c in corners[1:]):
        return None
    diff = np.abs(np.asarray(rgb).astype(int) - np.array(corners[0])).max(axis=2)
    cand = Image.fromarray(((diff <= tol) * 255).astype(np.uint8), "L").copy()   # .copy(): fromarray es de solo lectura
    for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if cand.getpixel(p) == 255:
            ImageDraw.floodfill(cand, p, 128)
    removed = np.asarray(cand) == 128
    if removed.mean() > 0.85:      # se comió casi todo: no era un fondo
        return None
    alpha = Image.fromarray((~removed * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(0.8))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    return out


def ingest_logo(data: bytes, remove_bg: bool = True) -> LogoResult:
    """Valida y normaliza un logo subido: RGBA recortado, ≤ 512 px, solo PNG/JPG/WebP."""
    if not data:
        raise BrandError("No se recibió ningún archivo")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BrandError("El logo pesa más de 5 MB")
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        if fmt not in ALLOWED_FORMATS:
            raise BrandError("Usa un logo PNG, JPG o WebP (mejor PNG con fondo transparente)")
        w, h = probe.size
        if min(w, h) < MIN_SIDE:
            raise BrandError(f"El logo es muy pequeño: que tenga al menos {MIN_SIDE} px de lado")
        if max(w, h) > MAX_SIDE:
            raise BrandError(f"El logo es muy grande: como mucho {MAX_SIDE} px de lado")
        if getattr(probe, "n_frames", 1) > 1:
            probe.seek(0)
        im = ImageOps.exif_transpose(probe)
        im = im.convert("RGBA")
        im.load()
    except BrandError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        if data[:512].lstrip().lower().startswith((b"<svg", b"<?xml", b"<!doctype svg")):
            raise BrandError("Usa un logo PNG, JPG o WebP (mejor PNG con fondo transparente)") from exc
        raise BrandError("No se pudo leer el logo: parece un archivo dañado o de otro tipo") from exc

    clean = im.getchannel("A").getextrema()[0] < 250       # ya trae transparencia
    if not clean and remove_bg:
        flat = _flat_background_to_alpha(im)
        if flat is not None:
            im, clean = flat, True
    bbox = im.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    if bbox is None:
        raise BrandError("El logo está vacío (todo transparente)")
    im = im.crop(bbox)
    margin = max(2, round(max(im.size) * 0.02))
    padded = Image.new("RGBA", (im.width + 2 * margin, im.height + 2 * margin), (0, 0, 0, 0))
    padded.paste(im, (margin, margin))
    im = padded
    if max(im.size) > LOGO_MAX:
        im.thumbnail((LOGO_MAX, LOGO_MAX), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return LogoResult(buf.getvalue(), im.width, im.height, clean, extract_colors(im))


def logo_is_dark(logo: Image.Image) -> bool:
    a = np.asarray(logo.convert("RGBA"))
    m = a[..., 3] > 128
    return bool(m.any()) and relative_luminance(tuple(a[m][:, :3].mean(axis=0))) < 0.22


def square(logo: Image.Image) -> Image.Image:
    s = max(logo.size)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(logo, ((s - logo.width) // 2, (s - logo.height) // 2))
    return out


# --- ajustes ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BrandSettings:
    style: str = DEFAULT_STYLE
    primary: str = DEFAULT_PRIMARY
    primary_auto: bool = False        # el color se sacó del logo (y se vuelve a sacar si se cambia el logo)
    short_name: str = ""              # vacío = el nombre del equipo del panel
    show_name: bool = False           # nombre en texto junto al logo (para logos sin letras)
    logo_digest: str = ""
    logo_clean: bool = False
    logo_size: tuple[int, int] = (0, 0)
    suggested: tuple[str, ...] = ()


def validate_color(value: str) -> str:
    if not isinstance(value, str) or not _HEX.match(value.strip()):
        raise BrandError("El color debe ser un valor hexadecimal como #D2AB43")
    return value.strip().upper()


def load_settings(db: Database) -> BrandSettings:
    raw = db.get_meta(META_KEY) or {}
    try:
        style = raw.get("style") if raw.get("style") in STYLES else DEFAULT_STYLE
        primary = validate_color(raw.get("primary", DEFAULT_PRIMARY))
    except BrandError:
        style, primary = DEFAULT_STYLE, DEFAULT_PRIMARY
    size = raw.get("logo_size") or (0, 0)
    return BrandSettings(
        style=style, primary=primary, primary_auto=bool(raw.get("primary_auto")),
        short_name=str(raw.get("short_name", ""))[:SHORT_NAME_MAX], show_name=bool(raw.get("show_name")),
        logo_digest=str(raw.get("logo_digest", "")), logo_clean=bool(raw.get("logo_clean")),
        logo_size=(int(size[0]), int(size[1])), suggested=tuple(raw.get("suggested") or ()),
    )


def save_settings(db: Database, s: BrandSettings) -> None:
    db.set_meta(META_KEY, {
        "v": 1, "style": s.style, "primary": s.primary, "primary_auto": s.primary_auto,
        "short_name": s.short_name, "show_name": s.show_name, "logo_digest": s.logo_digest,
        "logo_clean": s.logo_clean, "logo_size": list(s.logo_size), "suggested": list(s.suggested),
    })


def update_settings(db: Database, **changes) -> BrandSettings:
    """Cambia ajustes validándolos (el panel manda solo lo que cambió)."""
    cur = load_settings(db)
    values = {}
    if "style" in changes:
        if changes["style"] not in STYLES:
            raise BrandError("Estilo desconocido")
        values["style"] = changes["style"]
    if "primary" in changes:
        values["primary"] = validate_color(changes["primary"])
        values["primary_auto"] = bool(changes.get("primary_auto", False))
    if "short_name" in changes:
        values["short_name"] = " ".join(str(changes["short_name"]).split())[:SHORT_NAME_MAX]
    if "show_name" in changes:
        values["show_name"] = bool(changes["show_name"])
    new = BrandSettings(**{**cur.__dict__, **values})
    save_settings(db, new)
    return new


# --- logo en disco ------------------------------------------------------------------------------

def logo_path(cfg: Config) -> Path:
    return cfg.club_dir / "logo.png"


def save_logo(cfg: Config, db: Database, data: bytes, remove_bg: bool = True) -> BrandSettings:
    """Normaliza el logo subido, lo guarda y, si el color estaba en automático, lo saca de él."""
    result = ingest_logo(data, remove_bg)
    path = logo_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("logo.parte.png")
    tmp.write_bytes(result.png)
    os.replace(tmp, path)
    cur = load_settings(db)
    auto = cur.primary_auto or not cur.logo_digest      # primera vez: se propone el color del logo
    primary = result.colors[0] if (auto and result.colors) else cur.primary
    new = BrandSettings(**{**cur.__dict__, "primary": primary, "primary_auto": bool(auto and result.colors),
                           "logo_digest": result.digest, "logo_clean": result.clean,
                           "logo_size": (result.width, result.height), "suggested": tuple(result.colors)})
    save_settings(db, new)
    return new


def delete_logo(cfg: Config, db: Database) -> BrandSettings:
    logo_path(cfg).unlink(missing_ok=True)
    cur = load_settings(db)
    new = BrandSettings(**{**cur.__dict__, "logo_digest": "", "logo_clean": False, "logo_size": (0, 0),
                           "suggested": ()})
    save_settings(db, new)
    return new


# --- la marca lista para dibujar -----------------------------------------------------------------

@dataclass(frozen=True)
class Brand:
    settings: BrandSettings
    name: str                              # nombre que se muestra (corto o el del equipo)
    palette: Palette
    logo: Image.Image | None = None        # RGBA recortado (no cuadrado)
    hash: str = ""


def initials(name: str) -> str:
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+", name or "")
    if not words:
        return "FC"
    if len(words) == 1:
        return words[0][:4].upper() if len(words[0]) <= 4 else words[0][:3].upper()
    return "".join(w[0] for w in words[:3]).upper()


def make_brand(settings: BrandSettings, team: str, logo: Image.Image | None) -> Brand:
    name = (settings.short_name or team or "").strip()
    key = "|".join([settings.style, settings.primary, name, str(settings.show_name),
                    settings.logo_digest if logo is not None else "-", str(settings.logo_clean)])
    return Brand(settings, name, Palette(settings.primary), logo, hashlib.sha1(key.encode()).hexdigest()[:12])


def load_brand(cfg: Config, db: Database) -> Brand:
    """La marca actual. Nunca lanza: un logo dañado se ignora (con aviso en el registro)."""
    settings = load_settings(db)
    logo = None
    if settings.logo_digest:
        try:
            with Image.open(logo_path(cfg)) as f:
                logo = f.convert("RGBA")
        except (OSError, ValueError) as exc:
            log.warning("No se pudo abrir el logo del club (%s); se usa el escudo con iniciales", exc)
    return make_brand(settings, db.get_meta("team") or "", logo)
