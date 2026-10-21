"""Procesado de un VOD: descargar, detectar subidas de RKM y cortar clips. Más la sincronización con Twitch."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import ffmpeg
from .config import Config, Roi
from .db import Database
from .detector import Change, ChangeDetector, RkmReader, Templates, detect_changes, format_ts
from .downloader import download_vod, stream_url
from .twitch import TwitchClient

log = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    pass


class Cancelled(Exception):
    pass


@contextmanager
def single_instance(lock_path: Path):
    """Impide abrir la app dos veces (dos descargas del mismo VOD a la vez)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunning from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunning from exc
        yield
    finally:
        handle.close()  # cerrar el archivo libera el bloqueo


# --- configuración efectiva (con la calibración guardada en el panel) ----

# Cada parte de la calibración va en su propia clave de meta: así guardar una
# (p. ej. el fotograma de referencia) nunca puede pisar las demás.
CALIB_ROI, CALIB_NUMBER_BOX, CALIB_FRAME = "calib_roi", "calib_number_box", "calib_frame"


def effective_config(cfg: Config, db: Database) -> Config:
    roi = db.get_meta(CALIB_ROI)
    if roi:
        cfg = replace(cfg, roi=Roi(*roi))
    return cfg


def build_reader(cfg: Config, db: Database | None = None) -> RkmReader:
    number_box = db.get_meta(CALIB_NUMBER_BOX) if db else None
    templates = Templates.load(cfg.templates_dir, cfg.binarize)
    return RkmReader(
        templates,
        digits_width=cfg.digits_width,
        digits_pad=cfg.digits_pad,
        label_threshold=cfg.label_threshold,
        digit_threshold=cfg.digit_threshold,
        digit_margin=cfg.digit_margin,
        binarize=cfg.binarize,
        number_box=tuple(number_box) if number_box else None,
    )


def channel_login(cfg: Config, db: Database) -> str | None:
    if cfg.channel:
        return cfg.channel
    user = db.get_meta("user")
    return user["login"] if user else None


# --- clips --------------------------------------------------------------

def clip_window(change: Change, before: float, after: float, anchor: str = "last_old") -> tuple[float, float]:
    at = change.last_old_s if anchor == "last_old" else change.at_s
    return max(0.0, at - before), at + after


def clip_name(vod_id: str, created_at: str, change: Change) -> str:
    date = re.sub(r"[^0-9]", "", created_at[:10]) or "vod"
    ts = format_ts(change.last_old_s).replace(":", "-")
    return f"{date}_{vod_id}_{ts}_RKM{change.old}-{change.new}.mp4"


def analyze_video(cfg: Config, video: Path, reader: RkmReader, on_frame=None, on_unknown=None) -> list[Change]:
    frames = ffmpeg.iter_roi_frames(
        cfg.ffmpeg, video, cfg.ref_width, cfg.ref_height, cfg.roi, cfg.sample_fps, cfg.hwaccel
    )
    detector = ChangeDetector(stable_frames=cfg.stable_frames, max_jump=cfg.max_jump, on_suspicious=on_unknown)
    return detect_changes(frames, reader, detector, on_frame, on_unknown)


def cut_clips(cfg: Config, video: Path | str, vod_id: str, created_at: str, changes: list[Change],
              db: Database | None = None, on_clip=None) -> list[Path]:
    """video puede ser un archivo local o la URL del stream (solo se bajan los segundos del clip)."""
    out_dir = cfg.clips_dir / vod_id
    paths = []
    for i, change in enumerate(changes):
        start, end = clip_window(change, cfg.clip_before, cfg.clip_after, cfg.clip_anchor)
        out = out_dir / clip_name(vod_id, created_at, change)
        ffmpeg.cut_clip(cfg.ffmpeg, video, start, end, out, cfg.clip_reencode)
        thumb = out.with_suffix(".jpg")
        try:
            ffmpeg.grab_frame(cfg.ffmpeg, video, max(start, end - 1.5), 640, 360, thumb)
        except Exception:
            log.warning("No se pudo generar la miniatura de %s", out.name)
            thumb = None
        log.info("Clip guardado: %s", out)
        if db is not None:
            db.add_clip(vod_id, change, start, end, out, thumb)
        paths.append(out)
        if on_clip:
            on_clip(i + 1, len(changes))
    return paths


class _Progress:
    """Escribe el avance en la BD como mucho cada 2 s y comprueba la cancelación.

    yt-dlp llama a este aviso desde sus hilos de descarga de fragmentos, y una conexión
    SQLite solo puede usarse en el hilo que la creó: desde otro hilo se abre una propia.
    """

    def __init__(self, db: Database, vod_id: str, cancel: threading.Event | None):
        self.db, self.vod_id, self.cancel = db, vod_id, cancel
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._last = 0.0

    def __call__(self, fraction: float) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        fraction = max(0.0, min(1.0, fraction))
        with self._lock:
            now = time.monotonic()
            if now - self._last < 2:
                return
            self._last = now
        if threading.get_ident() == self._owner:
            self.db.set_progress(self.vod_id, fraction)
        else:
            other = Database(self.db.path)
            try:
                other.set_progress(self.vod_id, fraction)
            finally:
                other.close()


def process_vod(cfg: Config, db: Database, vod_id: str, cancel: threading.Event | None = None) -> None:
    row = db.get_vod(vod_id)
    if row is None:
        raise ValueError(f"VOD {vod_id} desconocido")
    cfg = effective_config(cfg, db)
    progress = _Progress(db, vod_id, cancel)
    video: Path | None = None

    db.start_attempt(vod_id)
    db.delete_clips(vod_id)  # en un reintento los clips se regeneran
    db.clear_unknowns(vod_id)
    try:
        reader = build_reader(cfg, db)
        # Para detectar basta una calidad baja (360p lee igual de bien y pesa ~4 veces menos);
        # los clips se cortan después del stream en máxima calidad.
        video = download_vod(row["url"], f"{vod_id}-analisis", cfg.vods_dir, cfg.analysis_format,
                             cfg.ffmpeg, cfg.ytdlp_fragments, on_progress=progress)

        db.set_status(vod_id, "analyzing")
        duration = row["duration_s"] or 1

        def on_unknown(at_s: float) -> None:
            if db.add_unknown(vod_id, at_s):
                log.warning("VOD %s, %s: se ve «RKM» pero el número no casa con ninguna plantilla",
                            vod_id, format_ts(at_s))

        changes = analyze_video(cfg, video, reader, on_frame=lambda t: progress(t / duration),
                                on_unknown=on_unknown)

        db.set_status(vod_id, "cutting")
        try:
            source: Path | str = stream_url(row["url"], cfg.ytdlp_format)
        except Exception:
            log.warning("No se pudo abrir el stream en alta calidad; los clips salen de la copia de análisis",
                        exc_info=True)
            source = video
        clips = cut_clips(cfg, source, vod_id, row["created_at"], changes, db,
                          on_clip=lambda i, n: progress(i / n))
        db.mark_done(vod_id, len(clips))
        log.info("VOD %s terminado: %d clips", vod_id, len(clips))
        if not cfg.keep_vods:
            video.unlink(missing_ok=True)
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            log.info("VOD %s cancelado", vod_id)
            db.set_status(vod_id, "cancelled")
        else:
            log.exception("Error procesando el VOD %s", vod_id)
            db.set_status(vod_id, "failed", error=f"{type(exc).__name__}: {exc}")


# --- sincronización con Twitch -------------------------------------------

def sync_vods(cfg: Config, db: Database, client: TwitchClient, login: str) -> dict:
    """Actualiza la lista de VODs del canal. Devuelve info del canal para el panel."""
    user_id = client.get_user_id(login)
    vods = client.get_archives(user_id, limit=30)
    live_stream = client.get_live_stream_id(user_id)
    new = 0
    for vod in vods:
        new += db.upsert_vod(vod)
    live_vod = next((v.id for v in vods if live_stream and v.stream_id == live_stream), None)
    state = {"live_vod": live_vod, "synced_at": time.time()}
    db.set_meta("sync", state)
    if new:
        log.info("%d VODs nuevos en %s", new, login)
    return state


def is_live_vod(db: Database, vod_id: str) -> bool:
    """El VOD del directo en curso sigue creciendo: no se puede procesar hasta que acabe."""
    return (db.get_meta("sync") or {}).get("live_vod") == vod_id

