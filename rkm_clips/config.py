"""Configuración leída del archivo .env (junto al proyecto) y de variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Cliente *público* de Twitch registrado para esta app: el ID no es un secreto (viaja en cada
# petición de login) y no hay secreto asociado. Quien bifurque el proyecto puede poner el suyo
# en TWITCH_CLIENT_ID.
DEFAULT_TWITCH_CLIENT_ID = "1jcobuumn04owfui7zbj63htcciab8"
APP_NAME = "DevStudio FC Clips"


class ConfigError(Exception):
    pass


def _str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(f"Falta {name} en .env")
        return default or ""
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número entero (vale {raw!r})") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número (vale {raw!r})") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "si", "sí", "on")


def _expand(raw: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return path if path.is_absolute() else PROJECT_DIR / path


def _path(name: str, default: str, legacy: str | None = None) -> Path:
    """Ruta de .env o la de por defecto. `legacy` es la de versiones anteriores: si existe y la
    nueva no, se sigue usando (así quien ya tenía datos no los pierde)."""
    raw = _str(name)
    if raw:
        return _expand(raw)
    new = _expand(default)
    if legacy and not new.exists() and _expand(legacy).exists():
        return _expand(legacy)
    return new


@dataclass(frozen=True)
class Roi:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Config:
    # Twitch
    client_id: str
    channel: str  # vacío = el canal de quien inicia sesión en el panel
    max_attempts: int
    poll_minutes: int
    host: str
    port: int

    # Rutas
    data_dir: Path
    clips_dir: Path
    templates_dir: Path

    # Herramientas
    ffmpeg: str
    ytdlp_format: str  # calidad de los clips
    analysis_format: str  # calidad que se descarga para detectar
    ytdlp_fragments: int
    hwaccel: str

    # Detección
    ref_width: int
    ref_height: int
    roi: Roi
    digits_width: int
    digits_pad: int
    label_threshold: float
    digit_threshold: float
    digit_margin: float
    sample_fps: float
    stable_frames: int
    max_jump: int
    binarize: bool

    # Clips
    clip_before: float
    clip_after: float
    clip_anchor: str  # last_old | first_new
    clip_reencode: bool
    keep_vods: bool

    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rkm_clips.sqlite3"

    @property
    def vods_dir(self) -> Path:
        return self.data_dir / "vods"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or PROJECT_DIR / ".env", override=False)

    data_dir = _path("DATA_DIR", "%LOCALAPPDATA%/DevStudioFCClips", legacy="%LOCALAPPDATA%/rkm-clips")
    return Config(
        client_id=_str("TWITCH_CLIENT_ID", DEFAULT_TWITCH_CLIENT_ID),
        channel=_str("TWITCH_CHANNEL").lower().lstrip("@"),
        max_attempts=_int("MAX_ATTEMPTS", 3),
        poll_minutes=_int("POLL_MINUTES", 20),
        host=_str("HOST", "127.0.0.1"),
        port=_int("PORT", 8765),
        data_dir=data_dir,
        clips_dir=_path("CLIPS_DIR", "%USERPROFILE%/Videos/FC Clips", legacy="%USERPROFILE%/Videos/RKM Clips"),
        # La calibración es del usuario: vive en sus datos, no en la carpeta de la app.
        templates_dir=_path("TEMPLATES_DIR", str(data_dir / "plantillas")),
        ffmpeg=_str("FFMPEG_PATH", "ffmpeg"),
        ytdlp_format=_str("YTDLP_FORMAT", "best[height<=1080]/best"),
        analysis_format=_str("ANALYSIS_FORMAT", "best[height<=480][height>=400]/best[height<=720]/best"),
        ytdlp_fragments=_int("YTDLP_FRAGMENTS", 4),
        hwaccel=_str("FFMPEG_HWACCEL", ""),
        ref_width=_int("REF_WIDTH", 1920),
        ref_height=_int("REF_HEIGHT", 1080),
        roi=Roi(
            x=_int("ROI_X", 0),
            y=_int("ROI_Y", 0),
            w=_int("ROI_W", 480),
            h=_int("ROI_H", 240),
        ),
        digits_width=_int("DIGITS_WIDTH", 160),
        digits_pad=_int("DIGITS_PAD", 6),
        label_threshold=_float("LABEL_THRESHOLD", 0.75),
        digit_threshold=_float("DIGIT_THRESHOLD", 0.90),
        digit_margin=_float("DIGIT_MARGIN", 0.08),
        sample_fps=_float("SAMPLE_FPS", 1.0),
        stable_frames=_int("STABLE_FRAMES", 2),
        max_jump=_int("MAX_JUMP", 3),
        binarize=_bool("BINARIZE", False),
        clip_before=_float("CLIP_BEFORE", 15.0),
        clip_after=_float("CLIP_AFTER", 3.0),
        clip_anchor=_str("CLIP_ANCHOR", "last_old").lower(),
        clip_reencode=_bool("CLIP_REENCODE", True),
        keep_vods=_bool("KEEP_VODS", False),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
