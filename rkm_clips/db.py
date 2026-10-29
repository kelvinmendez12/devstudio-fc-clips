"""Registro en SQLite: VODs del canal (con su estado), clips generados y ajustes del panel."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    created_at    TEXT,
    duration_s    INTEGER,
    url           TEXT,
    thumbnail_url TEXT,
    stream_id     TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    progress      REAL NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    clips         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    first_seen_at REAL NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vod_id        TEXT NOT NULL REFERENCES vods(id),
    change_s      REAL NOT NULL,
    start_s       REAL NOT NULL,
    end_s         REAL NOT NULL,
    old_value     INTEGER NOT NULL,
    new_value     INTEGER NOT NULL,
    path          TEXT NOT NULL,
    thumb_path    TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
-- Varios goles de un VOD unidos en un solo vídeo vertical (un solo borrador de TikTok).
CREATE TABLE IF NOT EXISTS compilations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vod_id           TEXT NOT NULL REFERENCES vods(id),
    clip_ids         TEXT NOT NULL,
    goals            INTEGER NOT NULL,
    duration_s       REAL NOT NULL DEFAULT 0,
    path             TEXT NOT NULL,
    draft_status     TEXT NOT NULL DEFAULT '',
    draft_error      TEXT,
    draft_publish_id TEXT,
    draft_sent_at    REAL,
    created_at       TEXT NOT NULL
);
-- Momentos con "RKM" visible y número no reconocido (dígito sin plantilla): posibles goles perdidos.
CREATE TABLE IF NOT EXISTS unknowns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vod_id        TEXT NOT NULL REFERENCES vods(id),
    at_s          REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | resolved | dismissed
    digit         INTEGER,
    created_at    TEXT NOT NULL
);
"""

MAX_UNKNOWNS_PER_VOD = 20

# Estados de un VOD:
#   new → queued → downloading → analyzing → cutting → done
#   cualquiera de los de trabajo → failed | cancelled
ACTIVE = ("queued", "downloading", "analyzing", "cutting")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Una conexión por hilo; WAL deja leer al panel mientras el worker escribe.
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Columnas añadidas después de la primera versión (SQLite no tiene ADD COLUMN IF NOT EXISTS)."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(clips)")}
        for name, ddl in (("tiktok_path", "TEXT"), ("tiktok_status", "TEXT NOT NULL DEFAULT ''"),
                          ("tiktok_error", "TEXT"),
                          # con qué diseño (marca + versión del dibujo) se hizo el vertical; '' = antes del nuevo diseño
                          ("tiktok_design", "TEXT NOT NULL DEFAULT ''"),
                          # Borrador en la cuenta de TikTok: '' → queued → uploading → sent | error
                          ("draft_status", "TEXT NOT NULL DEFAULT ''"), ("draft_error", "TEXT"),
                          ("draft_publish_id", "TEXT"), ("draft_sent_at", "REAL")):
            if name not in have:
                try:
                    self.conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {ddl}")
                except sqlite3.OperationalError:
                    pass  # otra instancia la añadió justo antes

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params=()) -> None:
        self.conn.execute(sql, params)
        self.conn.commit()

    # --- VODs ----------------------------------------------------------

    def upsert_vod(self, vod) -> bool:
        """Guarda o actualiza los datos de Twitch de un VOD. Devuelve True si es nuevo."""
        is_new = self.get_vod(vod.id) is None
        self._exec(
            "INSERT INTO vods (id, title, created_at, duration_s, url, thumbnail_url, stream_id,"
            " first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET title = excluded.title, duration_s = excluded.duration_s,"
            " thumbnail_url = excluded.thumbnail_url, url = excluded.url",
            (vod.id, vod.title, vod.created_at, vod.duration_s, vod.url, vod.thumbnail_url,
             vod.stream_id, time.time(), _now()),
        )
        return is_new

    def get_vod(self, vod_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM vods WHERE id = ?", (vod_id,)).fetchone()

    def list_vods(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM vods ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def set_status(self, vod_id: str, status: str, progress: float = 0.0, error: str | None = None) -> None:
        self._exec(
            "UPDATE vods SET status = ?, progress = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, progress, error[:2000] if error else None, _now(), vod_id),
        )

    def set_progress(self, vod_id: str, progress: float) -> None:
        self._exec("UPDATE vods SET progress = ? WHERE id = ?", (progress, vod_id))

    def start_attempt(self, vod_id: str) -> None:
        self._exec(
            "UPDATE vods SET status = 'downloading', progress = 0, attempts = attempts + 1,"
            " error = NULL, updated_at = ? WHERE id = ?",
            (_now(), vod_id),
        )

    def mark_done(self, vod_id: str, clips: int) -> None:
        self._exec(
            "UPDATE vods SET status = 'done', progress = 1, clips = ?, error = NULL, updated_at = ? WHERE id = ?",
            (clips, _now(), vod_id),
        )

    def reset_interrupted(self) -> int:
        """Tras un cierre inesperado, lo que estaba en marcha pasa a fallido para poder reintentarlo."""
        cur = self.conn.execute(
            f"UPDATE vods SET status = 'failed', error = 'Interrumpido (la app se cerró)'"
            f" WHERE status IN ({','.join('?' * len(ACTIVE))})",
            ACTIVE,
        )
        self.conn.commit()
        return cur.rowcount

    def auto_candidates(self, since: float, max_attempts: int) -> list[sqlite3.Row]:
        """VODs aparecidos después de activar el modo automático que aún no tienen clips."""
        return self.conn.execute(
            "SELECT * FROM vods WHERE first_seen_at > ? AND"
            " (status = 'new' OR (status = 'failed' AND attempts < ?))"
            " ORDER BY created_at",
            (since, max_attempts),
        ).fetchall()

    # --- clips ---------------------------------------------------------

    def add_clip(self, vod_id: str, change, start_s: float, end_s: float, path: Path, thumb: Path | None) -> None:
        self._exec(
            "INSERT INTO clips (vod_id, change_s, start_s, end_s, old_value, new_value, path, thumb_path, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # change_s = momento del gol (último segundo con el marcador anterior), el mismo del nombre del clip.
            (vod_id, change.last_old_s, start_s, end_s, change.old, change.new, str(path),
             str(thumb) if thumb else None, _now()),
        )

    # Versión vertical (TikTok): '' → queued → rendering → ready | error
    def set_tiktok(self, clip_id: int, status: str, path: Path | None = None, error: str | None = None,
                   design: str | None = None) -> None:
        self._exec(
            "UPDATE clips SET tiktok_status = ?, tiktok_path = COALESCE(?, tiktok_path), tiktok_error = ?,"
            " tiktok_design = COALESCE(?, tiktok_design) WHERE id = ?",
            (status, str(path) if path else None, error[:500] if error else None, design, clip_id),
        )

    def reset_tiktok_interrupted(self) -> int:
        cur = self.conn.execute(
            "UPDATE clips SET tiktok_status = '' WHERE tiktok_status IN ('queued', 'rendering')")
        self.conn.commit()
        return cur.rowcount

    def clips_missing_tiktok(self, vod_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clips WHERE vod_id = ? AND tiktok_status IN ('', 'error') ORDER BY change_s", (vod_id,)
        ).fetchall()

    def set_draft(self, clip_id: int, status: str, error: str | None = None, publish_id: str | None = None) -> None:
        sent_at = time.time() if status == "sent" else None
        self._exec(
            "UPDATE clips SET draft_status = ?, draft_error = ?, draft_publish_id = COALESCE(?, draft_publish_id),"
            " draft_sent_at = COALESCE(?, draft_sent_at) WHERE id = ?",
            (status, error[:500] if error else None, publish_id, sent_at, clip_id),
        )

    def reset_draft_interrupted(self) -> int:
        n = 0
        for table in ("clips", "compilations"):
            cur = self.conn.execute(f"UPDATE {table} SET draft_status = '' WHERE draft_status IN ('queued', 'uploading')")
            n += cur.rowcount
        self.conn.commit()
        return n

    def drafts_sent_since(self, since: float) -> int:
        """Borradores enviados desde `since` (TikTok admite 5 pendientes cada 24 h)."""
        return self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM clips WHERE draft_status = 'sent' AND draft_sent_at >= ?)"
            " + (SELECT COUNT(*) FROM compilations WHERE draft_status = 'sent' AND draft_sent_at >= ?)",
            (since, since),
        ).fetchone()[0]

    def clips_ready_for_draft(self, vod_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clips WHERE vod_id = ? AND tiktok_status = 'ready'"
            " AND draft_status IN ('', 'error') ORDER BY change_s", (vod_id,)
        ).fetchall()

    def delete_clips(self, vod_id: str) -> None:
        # Las compilaciones apuntan a clips concretos (a veces de varios directos): si alguno se regenera,
        # la compilación deja de valer.
        gone = {r["id"] for r in self.conn.execute("SELECT id FROM clips WHERE vod_id = ?", (vod_id,))}
        for comp in self.all_compilations():
            if gone & set(json.loads(comp["clip_ids"])):
                self.conn.execute("DELETE FROM compilations WHERE id = ?", (comp["id"],))
        self.conn.commit()
        self._exec("DELETE FROM clips WHERE vod_id = ?", (vod_id,))

    # --- compilaciones -------------------------------------------------

    def add_compilation(self, vod_id: str, clip_ids: list[int], path: Path, duration_s: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO compilations (vod_id, clip_ids, goals, duration_s, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (vod_id, json.dumps(clip_ids), len(clip_ids), duration_s, str(path), _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_compilation(self, comp_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM compilations WHERE id = ?", (comp_id,)).fetchone()

    def all_compilations(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM compilations ORDER BY id").fetchall()

    def compilations_for(self, vod_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM compilations WHERE vod_id = ? ORDER BY id", (vod_id,)).fetchall()

    def delete_compilation(self, comp_id: int) -> None:
        self._exec("DELETE FROM compilations WHERE id = ?", (comp_id,))

    def set_compilation_draft(self, comp_id: int, status: str, error: str | None = None,
                              publish_id: str | None = None) -> None:
        sent_at = time.time() if status == "sent" else None
        self._exec(
            "UPDATE compilations SET draft_status = ?, draft_error = ?,"
            " draft_publish_id = COALESCE(?, draft_publish_id), draft_sent_at = COALESCE(?, draft_sent_at) WHERE id = ?",
            (status, error[:500] if error else None, publish_id, sent_at, comp_id),
        )

    def clips_for(self, vod_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clips WHERE vod_id = ? ORDER BY change_s", (vod_id,)).fetchall()

    def get_clip(self, clip_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()

    # --- números sin reconocer -----------------------------------------

    def add_unknown(self, vod_id: str, at_s: float) -> bool:
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM unknowns WHERE vod_id = ? AND status = 'pending'", (vod_id,)
        ).fetchone()[0]
        if pending >= MAX_UNKNOWNS_PER_VOD:
            return False  # con unos cuantos ejemplos basta para calibrar lo que falta
        self._exec("INSERT INTO unknowns (vod_id, at_s, created_at) VALUES (?, ?, ?)", (vod_id, at_s, _now()))
        return True

    def clear_unknowns(self, vod_id: str) -> None:
        self._exec("DELETE FROM unknowns WHERE vod_id = ? AND status = 'pending'", (vod_id,))

    def pending_unknowns(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT u.*, v.title, v.created_at AS vod_created_at FROM unknowns u"
            " JOIN vods v ON v.id = u.vod_id WHERE u.status = 'pending' ORDER BY v.created_at DESC, u.at_s"
        ).fetchall()

    def unknowns_for(self, vod_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM unknowns WHERE vod_id = ? AND status = 'pending'", (vod_id,)
        ).fetchone()[0]

    def get_unknown(self, unknown_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM unknowns WHERE id = ?", (unknown_id,)).fetchone()

    def set_unknown_status(self, unknown_id: int, status: str, digit: int | None = None) -> None:
        self._exec("UPDATE unknowns SET status = ?, digit = ? WHERE id = ?", (status, digit, unknown_id))

    # --- ajustes y token -----------------------------------------------

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self._exec(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def delete_meta(self, key: str) -> None:
        self._exec("DELETE FROM meta WHERE key = ?", (key,))

    def get_user_token(self) -> dict | None:
        """{"access", "refresh", "expires_at"} de la sesión de Twitch, o None si no hay."""
        return self.get_meta("twitch_token")

    def save_user_token(self, access: str, refresh: str, expires_at: float) -> None:
        self.set_meta("twitch_token", {"access": access, "refresh": refresh, "expires_at": expires_at})

    def clear_user_token(self) -> None:
        self.delete_meta("twitch_token")
        self.delete_meta("user")

    # La sesión de TikTok es independiente de la de Twitch.
    def get_tiktok_token(self) -> dict | None:
        """{"access", "refresh", "expires_at", "refresh_expires_at", "open_id"} o None."""
        return self.get_meta("tiktok_token")

    def save_tiktok_token(self, token: dict) -> None:
        self.set_meta("tiktok_token", token)

    def clear_tiktok(self) -> None:
        self.delete_meta("tiktok_token")
        self.delete_meta("tiktok_user")
