"""Hilos de fondo del panel: cola de VODs a procesar (de uno en uno) y consulta periódica a Twitch."""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import updater
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

    def sync_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self._process_loop, name="rkm-worker", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="rkm-poller", daemon=True).start()

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
            except Exception:
                log.exception("Fallo inesperado con el VOD %s", vod_id)
            finally:
                with self._lock:
                    self._current = None

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
