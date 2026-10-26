"""Hilos de fondo del panel: cola de VODs a procesar (de uno en uno) y consulta periódica a Twitch."""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import tiktok, updater
from .config import Config
from .db import Database
from .pipeline import channel_login, is_live_vod, process_vod, sync_vods
from .twitch import NotLoggedIn, TwitchClient

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._renders: queue.Queue[int] = queue.Queue()
        self._render_pending: set[int] = set()
        self._sends: queue.Queue[tuple[str, int]] = queue.Queue()
        self._send_pending: set[tuple[str, int]] = set()
        self._current: str | None = None
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_sync_error: str | None = None
        self.needs_login = False  # la sesión de Twitch caducó o se revocó: hay que reconectar

    # --- API para el panel ---------------------------------------------

    def enqueue(self, db: Database, vod_id: str) -> bool:
        with self._lock:
            if vod_id in self._pending or vod_id == self._current:
                return False
            self._pending.add(vod_id)
        db.set_status(vod_id, "queued")
        self._queue.put(vod_id)
        return True

    def cancel(self, db: Database, vod_id: str) -> bool:
        with self._lock:
            if vod_id == self._current:
                self._cancel.set()
                return True
            if vod_id in self._pending:
                self._pending.discard(vod_id)  # el hilo lo saltará al sacarlo de la cola
                db.set_status(vod_id, "cancelled")
                return True
        return False

    def enqueue_render(self, db: Database, clip_id: int) -> bool:
        """Pide la versión vertical (TikTok) de un clip; se hacen de una en una."""
        with self._lock:
            if clip_id in self._render_pending:
                return False
            self._render_pending.add(clip_id)
        db.set_tiktok(clip_id, "queued")
        self._renders.put(clip_id)
        return True

    def enqueue_renders_for_vod(self, db: Database, vod_id: str) -> int:
        n = 0
        for clip in db.clips_missing_tiktok(vod_id):
            n += self.enqueue_render(db, clip["id"])
        return n

    def enqueue_redesign_for_vod(self, db: Database, vod_id: str) -> int:
        """Rehace con el diseño actual los verticales hechos con otro. No toca los ya enviados a TikTok."""
        design = tiktok.current_design(self.cfg, db)
        n = 0
        for clip in db.clips_for(vod_id):
            if (clip["tiktok_status"] == "ready" and clip["tiktok_design"] != design
                    and clip["draft_status"] not in ("queued", "uploading", "sent")):
                n += self.enqueue_render(db, clip["id"])
        return n

    def enqueue_send(self, db: Database, clip_id: int) -> bool:
        """Pide enviar la versión vertical de un clip como borrador a TikTok (de una en una)."""
        with self._lock:
            if ("clip", clip_id) in self._send_pending:
                return False
            self._send_pending.add(("clip", clip_id))
        db.set_draft(clip_id, "queued")
        self._sends.put(("clip", clip_id))
        return True

    def enqueue_send_compilation(self, db: Database, comp_id: int) -> bool:
        with self._lock:
            if ("comp", comp_id) in self._send_pending:
                return False
            self._send_pending.add(("comp", comp_id))
        db.set_compilation_draft(comp_id, "queued")
        self._sends.put(("comp", comp_id))
        return True

    def enqueue_sends_for_vod(self, db: Database, vod_id: str) -> int:
        """Envía los clips listos del VOD sin pasar del límite diario de TikTok. Devuelve cuántos."""
        left = tiktok.drafts_left(db)
        n = 0
        for clip in db.clips_ready_for_draft(vod_id):
            if n >= left:
                break
            n += self.enqueue_send(db, clip["id"])
        return n

    def sync_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self._send_loop, name="rkm-tiktok-send", daemon=True).start()
        threading.Thread(target=self._process_loop, name="rkm-worker", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="rkm-poller", daemon=True).start()
        threading.Thread(target=self._render_loop, name="rkm-render", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()

    # --- hilos ---------------------------------------------------------

    def _process_loop(self) -> None:
        db = Database(self.cfg.db_path)
        while not self._stop.is_set():
            try:
                vod_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            with self._lock:
                if vod_id not in self._pending:
                    continue  # cancelado mientras esperaba
                self._pending.discard(vod_id)
                self._current = vod_id
                self._cancel.clear()
            try:
                process_vod(self.cfg, db, vod_id, self._cancel)
                row = db.get_vod(vod_id)
                if self.cfg.tiktok_auto and row and row["status"] == "done":
                    n = self.enqueue_renders_for_vod(db, vod_id)
                    if n:
                        log.info("VOD %s: %d versiones verticales en cola", vod_id, n)
            except Exception:
                log.exception("Fallo inesperado con el VOD %s", vod_id)
            finally:
                with self._lock:
                    self._current = None

    def _render_loop(self) -> None:
        db = Database(self.cfg.db_path)
        while not self._stop.is_set():
            try:
                clip_id = self._renders.get(timeout=1)
            except queue.Empty:
                continue
            try:
                tiktok.render_clip(self.cfg, db, clip_id)
            except Exception:
                log.exception("Fallo inesperado creando la versión vertical del clip %s", clip_id)
            finally:
                with self._lock:
                    self._render_pending.discard(clip_id)

    def _send_loop(self) -> None:
        db = Database(self.cfg.db_path)
        while not self._stop.is_set():
            try:
                kind, item_id = self._sends.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if kind == "comp":
                    tiktok.send_compilation(self.cfg, db, item_id)
                else:
                    tiktok.send_draft(self.cfg, db, item_id)
            except Exception:
                log.exception("Fallo inesperado enviando %s %s a TikTok", kind, item_id)
                message = "Fallo inesperado; revisa el registro"
                if kind == "comp":
                    db.set_compilation_draft(item_id, "error", error=message)
                else:
                    db.set_draft(item_id, "error", error=message)
            finally:
                with self._lock:
                    self._send_pending.discard((kind, item_id))
            self._stop.wait(11)  # init admite 6 peticiones/min por usuario: margen de sobra

    def _poll_loop(self) -> None:
        db = Database(self.cfg.db_path)
        client = TwitchClient(self.cfg.client_id, token_store=db)
        while not self._stop.is_set():
            login = channel_login(self.cfg, db)
            if login:
                try:
                    sync_vods(self.cfg, db, client, login)
                    self.last_sync_error = None
                    self.needs_login = False
                    self._auto_enqueue(db)
                except Exception as exc:
                    self.last_sync_error = str(exc)
                    self.needs_login = isinstance(exc, NotLoggedIn)
                    log.warning("No se pudo consultar Twitch: %s", exc)
            try:
                updater.check(db)  # como mucho una consulta a GitHub cada 6 h
            except Exception:
                log.debug("Comprobación de actualizaciones fallida", exc_info=True)
            self._wake.wait(self.cfg.poll_minutes * 60)
            self._wake.clear()

    def _auto_enqueue(self, db: Database) -> None:
        auto = db.get_meta("auto") or {}
        if not auto.get("enabled"):
            return
        for row in db.auto_candidates(auto["since"], self.cfg.max_attempts):
            if not is_live_vod(db, row["id"]) and self.enqueue(db, row["id"]):
                log.info("Modo automático: VOD %s en cola", row["id"])


def set_auto(db: Database, enabled: bool) -> dict:
    # Solo cuentan los VODs que aparezcan desde ahora; los anteriores se generan a mano.
    current = db.get_meta("auto") or {}
    if enabled and not current.get("enabled"):
        current = {"enabled": True, "since": time.time()}
    elif not enabled:
        current = {"enabled": False, "since": current.get("since", 0)}
    db.set_meta("auto", current)
    return current
