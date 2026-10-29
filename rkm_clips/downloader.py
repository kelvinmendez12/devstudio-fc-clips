"""Descarga de VODs con yt-dlp (como librería, con reanudación de .part)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)


class _YtdlpLogger:
    def debug(self, msg):  # yt-dlp manda aquí también los mensajes informativos
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


def _ffmpeg_location(ffmpeg: str) -> str | None:
    resolved = shutil.which(ffmpeg)
    return str(Path(resolved).parent) if resolved else None


def _progress_hook(on_progress):
    def hook(d):
        if d.get("status") != "downloading":
            return
        # En HLS (Twitch) el total en bytes suele ser estimado; los fragmentos son más fiables.
        if d.get("fragment_count"):
            on_progress((d.get("fragment_index") or 0) / d["fragment_count"])
        elif d.get("total_bytes") or d.get("total_bytes_estimate"):
            on_progress(d.get("downloaded_bytes", 0) / (d.get("total_bytes") or d["total_bytes_estimate"]))
    return hook


def _network_opts() -> dict:
    """Reintentos con espera creciente: si la conexión se corta (Twitch o el router cierran el socket) no se
    da el VOD por perdido al primer fallo."""
    backoff = lambda n: min(2 ** n, 30)  # noqa: E731
    return {
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 8,
        "socket_timeout": 30,
        "retry_sleep_functions": {"http": backoff, "fragment": backoff, "extractor": backoff},
    }


def download_vod(url: str, vod_id: str, dest_dir: Path, fmt: str, ffmpeg: str, fragments: int = 4,
                 on_progress=None) -> Path:
    """on_progress(fracción 0–1) puede lanzar una excepción para cancelar la descarga."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": fmt,
        "outtmpl": str(dest_dir / f"{vod_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": fragments,
        **_network_opts(),
        "continuedl": True,
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        "logger": _YtdlpLogger(),
    }
    if on_progress:
        opts["progress_hooks"] = [_progress_hook(on_progress)]
    location = _ffmpeg_location(ffmpeg)
    if location:
        opts["ffmpeg_location"] = location

    log.info("Descargando VOD %s (%s)", vod_id, url)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads else Path(ydl.prepare_filename(info))

    if not path.exists():
        raise FileNotFoundError(f"yt-dlp terminó pero no aparece el archivo {path}")
    log.info("VOD %s descargado: %s (%.1f GB)", vod_id, path.name, path.stat().st_size / 1e9)
    return path


def stream_url(url: str, fmt: str) -> str:
    """URL directa (m3u8) de un VOD, para sacar fotogramas sueltos sin descargarlo entero."""
    with YoutubeDL({"format": fmt, "quiet": True, "logger": _YtdlpLogger(), **_network_opts()}) as ydl:
        info = ydl.extract_info(url, download=False)
    return info["url"]
