"""Versión vertical de los clips para TikTok: se genera sola al terminar cada VOD (o con el botón del panel)."""

from __future__ import annotations

import logging
from pathlib import Path

import time

from .config import Config
from .db import Database
from .pipeline import channel_login
from .tiktok_api import DRAFT_LIMIT, DRAFT_WINDOW_S, NotConnected, TikTokClient, TikTokError
from .vertical import VerticalOptions, make_caption, render_vertical

log = logging.getLogger(__name__)


def options(cfg: Config) -> VerticalOptions:
    return VerticalOptions(
        lead_s=cfg.tiktok_lead,
        replay_before_s=cfg.tiktok_replay_before,
        replay_after_s=cfg.tiktok_replay_after,
        replay_slow=cfg.tiktok_replay_slow,
        tagline=cfg.tiktok_tagline,
    )


def vertical_path(clip_path: Path) -> Path:
    return clip_path.with_name(clip_path.stem + "_tiktok.mp4")


def caption_for(cfg: Config, db: Database, clip) -> str:
    return make_caption(db.get_meta("team") or "", clip["new_value"], channel_login(cfg, db) or "",
                        cfg.tiktok_hashtags)


def client_for(cfg: Config, db: Database) -> TikTokClient:
    return TikTokClient(cfg.tiktok_client_key, cfg.tiktok_client_secret, cfg.tiktok_redirect_uri,
                        token_store=db)


def drafts_left(db: Database) -> int:
    """Cuántos borradores más admite TikTok ahora (5 por cuenta y día, contando los que enviamos)."""
    return max(0, DRAFT_LIMIT - db.drafts_sent_since(time.time() - DRAFT_WINDOW_S))


def send_draft(cfg: Config, db: Database, clip_id: int, client: TikTokClient | None = None) -> bool:
    """Sube la versión vertical de un clip como borrador a la cuenta de TikTok conectada."""
    clip = db.get_clip(clip_id)
    if clip is None:
        return False
    client = client or client_for(cfg, db)
    db.set_draft(clip_id, "uploading")
    try:
        path = Path(clip["tiktok_path"] or "")
        if clip["tiktok_status"] != "ready" or not path.is_file():
            raise TikTokError("Primero hay que crear la versión vertical del clip")
        publish_id = client.upload_draft(path)
        db.set_draft(clip_id, "uploading", publish_id=publish_id)
        client.wait_for_inbox(publish_id)
    except NotConnected as exc:
        db.set_draft(clip_id, "error", error=str(exc))
        return False
    except Exception as exc:
        log.warning("No se pudo enviar el clip %s a TikTok: %s", clip_id, exc)
        db.set_draft(clip_id, "error", error=str(exc))
        return False
    db.set_draft(clip_id, "sent")
    log.info("Clip %s enviado a TikTok como borrador", clip_id)
    return True


def render_clip(cfg: Config, db: Database, clip_id: int) -> Path | None:
    """Genera la versión vertical de un clip y guarda el resultado en la base de datos."""
    clip = db.get_clip(clip_id)
    if clip is None:
        return None
    src = Path(clip["path"])
    db.set_tiktok(clip_id, "rendering")
    try:
        if not src.exists():
            raise FileNotFoundError("El clip original ya no está en la carpeta")
        out = render_vertical(
            cfg.ffmpeg, src, vertical_path(src),
            goal_s=clip["change_s"] - clip["start_s"],
            old=clip["old_value"], new=clip["new_value"],
            team=db.get_meta("team") or "", channel=channel_login(cfg, db) or "",
            opts=options(cfg),
        )
    except Exception as exc:
        log.exception("No se pudo crear la versión TikTok del clip %s", clip_id)
        db.set_tiktok(clip_id, "error", error=f"{type(exc).__name__}: {exc}")
        return None
    db.set_tiktok(clip_id, "ready", path=out)
    log.info("Versión TikTok lista: %s", out.name)
    return out
