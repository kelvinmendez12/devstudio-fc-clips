"""Versión vertical de los clips para TikTok: se genera sola al terminar cada VOD (o con el botón del panel)."""

from __future__ import annotations

import logging
from pathlib import Path

import json
import time

from .config import Config
from .db import Database
from .pipeline import channel_login
from .tiktok_api import DRAFT_LIMIT, DRAFT_WINDOW_S, NotConnected, TikTokClient, TikTokError
from .vertical import VerticalOptions, join_videos, make_caption, make_compilation_caption, render_vertical

log = logging.getLogger(__name__)

MAX_COMPILATION = 10


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


def _upload(client: TikTokClient, path: Path, set_draft) -> bool:
    """Sube un vídeo como borrador y espera a que esté en la bandeja; `set_draft` guarda el estado."""
    set_draft("uploading")
    try:
        if not path.is_file():
            raise TikTokError("El vídeo vertical ya no está en la carpeta")
        publish_id = client.upload_draft(path)
        set_draft("uploading", publish_id=publish_id)
        client.wait_for_inbox(publish_id)
    except NotConnected as exc:
        set_draft("error", error=str(exc))
        return False
    except Exception as exc:
        log.warning("No se pudo enviar %s a TikTok: %s", path.name, exc)
        set_draft("error", error=str(exc))
        return False
    set_draft("sent")
    log.info("%s enviado a TikTok como borrador", path.name)
    return True


def send_draft(cfg: Config, db: Database, clip_id: int, client: TikTokClient | None = None) -> bool:
    """Sube la versión vertical de un clip como borrador a la cuenta de TikTok conectada."""
    clip = db.get_clip(clip_id)
    if clip is None:
        return False
    client = client or client_for(cfg, db)
    set_draft = lambda status, **kw: db.set_draft(clip_id, status, **kw)  # noqa: E731
    if clip["tiktok_status"] != "ready" or not clip["tiktok_path"]:
        set_draft("error", error="Primero hay que crear la versión vertical del clip")
        return False
    return _upload(client, Path(clip["tiktok_path"]), set_draft)


def send_compilation(cfg: Config, db: Database, comp_id: int, client: TikTokClient | None = None) -> bool:
    comp = db.get_compilation(comp_id)
    if comp is None:
        return False
    client = client or client_for(cfg, db)
    return _upload(client, Path(comp["path"]),
                   lambda status, **kw: db.set_compilation_draft(comp_id, status, **kw))


def compilation_path(cfg: Config, vod_id: str, clip_ids: list[int]) -> Path:
    stamp = time.strftime("%H%M%S")
    return cfg.clips_dir / f"compilacion_{vod_id}_{'-'.join(map(str, clip_ids))}_{stamp}_tiktok.mp4"


def compilation_caption(cfg: Config, db: Database, comp) -> str:
    return make_compilation_caption(db.get_meta("team") or "", comp["goals"], channel_login(cfg, db) or "",
                                    cfg.tiktok_hashtags)


def make_compilation(cfg: Config, db: Database, vod_id: str, clip_ids: list[int]) -> int:
    """Junta las versiones verticales de varios goles (en orden de partido) en un solo vídeo."""
    clips = {c["id"]: c for c in db.clips_for(vod_id)}
    chosen = []
    for cid in set(clip_ids):
        clip = clips.get(cid)
        if clip is None:
            raise TikTokError("Uno de los goles no es de este VOD")
        if clip["tiktok_status"] != "ready" or not clip["tiktok_path"] or not Path(clip["tiktok_path"]).is_file():
            raise TikTokError("Todos los goles elegidos necesitan su versión vertical (créala primero)")
        chosen.append(clip)
    if len(chosen) < 2:
        raise TikTokError("Elige al menos dos goles para juntarlos")
    if len(chosen) > MAX_COMPILATION:
        raise TikTokError(f"Como mucho {MAX_COMPILATION} goles por vídeo")
    chosen.sort(key=lambda c: c["change_s"])
    ids = [c["id"] for c in chosen]
    out = compilation_path(cfg, vod_id, ids)
    used = {r["path"] for r in db.compilations_for(vod_id)}
    n = 1
    while str(out) in used:  # nunca se reutiliza la ruta de una ya registrada (aunque esté oculta)
        n += 1
        out = out.with_name(out.stem.removesuffix("_tiktok").rsplit("-v", 1)[0] + f"-v{n}_tiktok.mp4")
    duration = join_videos(cfg.ffmpeg, [Path(c["tiktok_path"]) for c in chosen], out)
    for old in db.compilations_for(vod_id):  # misma selección: la nueva sustituye a la anterior
        if json.loads(old["clip_ids"]) != ids:
            continue
        Path(old["path"]).unlink(missing_ok=True)
        if old["draft_status"] != "sent":
            db.delete_compilation(old["id"])
        # Una ya enviada se queda (sin archivo, oculta) para que el cupo diario cuente ese envío.
    return db.add_compilation(vod_id, ids, out, duration)


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
