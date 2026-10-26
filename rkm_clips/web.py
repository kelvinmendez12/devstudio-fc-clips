"""Panel web local: login con Twitch, lista de VODs, botón «Generar clips», galería y calibración."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import cv2
import numpy as np
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

from . import ffmpeg, tiktok, updater
from . import brand as brand_mod, scoreboard_art
from .config import APP_NAME, Config
from .db import ACTIVE, Database
from .detector import Templates, format_ts, imread_gray, imwrite, preprocess
from .downloader import stream_url
from .pipeline import (
    CALIB_FRAME, CALIB_NUMBER_BOX, CALIB_ROI, build_reader, channel_login, effective_config, is_live_vod,
)
from .tiktok_api import DRAFT_LIMIT, NotConnected, TikTokError, new_verifier
from .vertical import VerticalError
from .twitch import TwitchClient, TwitchError, thumbnail
from .worker import Worker, set_auto

log = logging.getLogger(__name__)


def trim_glyph(img: np.ndarray) -> np.ndarray:
    """Ajusta el recorte al texto (con 1 px de margen) para que la plantilla no arrastre fondo.

    Se queda con las formas grandes (el dígito, o las letras de «RKM») y descarta motas y las
    líneas finas pegadas al borde, como el filo del recuadro del marcador.
    """
    _, mask = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    fg = mask > 0
    if fg.mean() > 0.5:  # texto oscuro sobre fondo claro
        fg = ~fg
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8), connectivity=8)
    if n > 1:
        h, w = fg.shape
        biggest = stats[1:, cv2.CC_STAT_AREA].max()
        keep = np.zeros_like(fg)
        for i in range(1, n):
            x, y, cw, ch, area = stats[i]
            touches_edge = x == 0 or y == 0 or x + cw == w or y + ch == h
            if area < 0.15 * biggest or (touches_edge and min(cw, ch) <= 3):
                continue
            keep |= labels == i
        if keep.any():
            fg = keep
    ys, xs = np.nonzero(fg)
    if not len(xs):
        return img
    y0, y1 = max(0, ys.min() - 1), min(img.shape[0], ys.max() + 2)
    x0, x1 = max(0, xs.min() - 1), min(img.shape[1], xs.max() + 2)
    return img[y0:y1, x0:x1].copy()


def create_app(cfg: Config, worker: Worker | None = None) -> Flask:
    app = Flask(__name__)
    boot_db = Database(cfg.db_path)
    secret = boot_db.get_meta("secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        boot_db.set_meta("secret_key", secret)
    boot_db.close()
    app.secret_key = secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Las cookies de localhost se comparten entre puertos: un nombre genérico como
    # "session" lo pisaría cualquier otra app local.
    app.config["SESSION_COOKIE_NAME"] = "fc_clips_session"
    app.config["MAX_CONTENT_LENGTH"] = brand_mod.MAX_UPLOAD_BYTES + 512 * 1024   # el logo es lo único que se sube

    @app.errorhandler(413)
    def _too_big(_exc):
        return jsonify(error="El archivo pesa más de 5 MB"), 413
    worker = worker or Worker(cfg)
    app.extensions["rkm_worker"] = worker
    calib_frame = cfg.data_dir / "calibration" / "frame.png"

    def db() -> Database:
        if "db" not in g:
            g.db = Database(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def twitch() -> TwitchClient:
        return TwitchClient(cfg.client_id, token_store=db())

    def current_user():
        user = db().get_meta("user")
        if user and session.get("uid") == user["id"]:
            return user
        return None

    def rejection_reason() -> str:
        user = db().get_meta("user")
        if "uid" not in session:
            return "sin cookie de sesión"
        if not user:
            return "no hay usuario guardado en la BD"
        return "la sesión es de otro usuario"

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if current_user() is None:
                log.warning("Petición sin sesión válida a %s %s: %s", request.method, request.path,
                            rejection_reason())
                if request.path.startswith("/api/") or request.path.startswith("/media/"):
                    return jsonify(error="Inicia sesión con Twitch"), 401
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapper

    @app.before_request
    def _canonical_host():
        # La cookie de sesión es por nombre de host: se unifica todo en "localhost".
        if request.host.split(":")[0] == "127.0.0.1":
            return redirect(request.url.replace("://127.0.0.1", "://localhost", 1))

    # --- páginas -------------------------------------------------------

    @app.get("/")
    def index():
        user = current_user()
        if user is None:
            return render_template("login.html", error=request.args.get("error"), app_name=APP_NAME)
        return render_template("panel.html", user=user)

    @app.get("/club")
    @login_required
    def club_page():
        return render_template("club.html", user=current_user())

    @app.get("/calibrar")
    @login_required
    def calibrate_page():
        return render_template("calibrate.html", user=current_user(), cfg=cfg)

    # --- conectar con Twitch (código de dispositivo, sin secreto) -------

    @app.post("/auth/device/start")
    def auth_device_start():
        try:
            device = twitch().start_device_login()
        except TwitchError as exc:
            return jsonify(error=str(exc)), 502
        session["device"] = {"code": device.device_code, "expires_at": time.time() + device.expires_in}
        return jsonify(user_code=device.user_code, verification_uri=device.verification_uri,
                       expires_in=device.expires_in, interval=device.interval)

    @app.post("/auth/device/poll")
    def auth_device_poll():
        device = session.get("device")
        if not device or device["expires_at"] < time.time():
            session.pop("device", None)
            return jsonify(status="expired", error="El código caducó; pide uno nuevo"), 410
        try:
            user = twitch().poll_device_login(device["code"])
        except TwitchError as exc:
            session.pop("device", None)
            return jsonify(status="error", error=str(exc)), 400
        if user is None:
            return jsonify(status="pending")
        session.pop("device", None)
        db().set_meta("user", user.__dict__)
        session["uid"] = user.id
        session.permanent = True
        log.info("Cuenta de Twitch conectada: %s", user.login)
        worker.sync_now()
        return jsonify(status="ok", login=user.login)

    @app.post("/auth/logout")
    def auth_logout():
        # Desconectar de verdad: se olvida el token de Twitch, no solo la cookie.
        db().clear_user_token()
        session.clear()
        return redirect(url_for("index"))

    # --- conectar con TikTok (Login Kit de escritorio, con PKCE) ---------

    def tiktok_back(**params):
        return redirect(url_for("index") + ("?" + urlencode(params) if params else ""))

    @app.get("/auth/tiktok/start")
    @login_required
    def auth_tiktok_start():
        if not cfg.tiktok_configured:
            return tiktok_back(tiktok_error="Faltan TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en el archivo .env")
        state, verifier = secrets.token_urlsafe(24), new_verifier()
        session["tiktok"] = {"state": state, "verifier": verifier}
        return redirect(tiktok.client_for(cfg, db()).authorize_url(state, verifier))

    @app.get("/auth/tiktok/callback")
    @login_required
    def auth_tiktok_callback():
        pending = session.pop("tiktok", None)
        if request.args.get("error"):
            return tiktok_back(tiktok_error=request.args.get("error_description") or request.args["error"])
        if not pending or not secrets.compare_digest(pending["state"], request.args.get("state", "")):
            return tiktok_back(tiktok_error="La respuesta de TikTok no corresponde a esta sesión; inténtalo de nuevo")
        try:
            user = tiktok.client_for(cfg, db()).exchange_code(request.args.get("code", ""), pending["verifier"])
        except TikTokError as exc:
            log.warning("Login de TikTok fallido: %s", exc)
            db().clear_tiktok()
            return tiktok_back(tiktok_error=str(exc))
        db().set_meta("tiktok_user", user.__dict__)
        log.info("Cuenta de TikTok conectada: %s", user.display_name)
        return tiktok_back(tiktok="ok")

    @app.post("/auth/tiktok/logout")
    @login_required
    def auth_tiktok_logout():
        db().clear_tiktok()
        return jsonify(ok=True)

    def tiktok_state() -> dict:
        d = db()
        user = d.get_meta("tiktok_user")
        return {
            "configured": cfg.tiktok_configured,
            "connected": bool(d.get_tiktok_token() and user),
            "user": (user or {}).get("display_name", ""),
            "left": tiktok.drafts_left(d),
            "limit": DRAFT_LIMIT,
        }

    # --- API del panel -------------------------------------------------

    def vod_json(row) -> dict:
        clips = [
            {
                "id": c["id"],
                "old": c["old_value"],
                "new": c["new_value"],
                "at": format_ts(c["change_s"]),
                "duration": round(c["end_s"] - c["start_s"]),
                "name": Path(c["path"]).name,
                "exists": Path(c["path"]).exists(),
                "has_thumb": bool(c["thumb_path"]) and Path(c["thumb_path"]).exists(),
                "tiktok": {
                    # «ready» solo si el archivo sigue ahí (puede haberse borrado a mano)
                    "status": "ready" if c["tiktok_status"] == "ready" and c["tiktok_path"]
                              and Path(c["tiktok_path"]).exists() else
                              ("" if c["tiktok_status"] == "ready" else c["tiktok_status"]),
                    "error": c["tiktok_error"],
                    "caption": tiktok.caption_for(cfg, db(), c),
                },
                "draft": {"status": c["draft_status"], "error": c["draft_error"]},
            }
            for c in db().clips_for(row["id"])
        ]
        compilations = [
            {
                "id": k["id"],
                "goals": k["goals"],
                "duration": format_ts(k["duration_s"]),
                "exists": Path(k["path"]).exists(),
                "caption": tiktok.compilation_caption(cfg, db(), k),
                "draft": {"status": k["draft_status"], "error": k["draft_error"]},
            }
            for k in db().compilations_for(row["id"])
        ]
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "duration": format_ts(row["duration_s"] or 0),
            "thumbnail": thumbnail(row["thumbnail_url"], 640, 360),
            "url": row["url"],
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
            "attempts": row["attempts"],
            "live": is_live_vod(db(), row["id"]),
            "unknowns": db().unknowns_for(row["id"]),
            "clips": clips,
            "compilations": compilations,
        }

    @app.get("/api/state")
    @login_required
    def api_state():
        d = db()
        try:
            templates = Templates.load(cfg.templates_dir, cfg.binarize)
            calibrated = {"label": True, "digits": sorted(templates.digits)}
        except Exception:
            calibrated = {"label": (cfg.templates_dir / "rkm_label.png").exists(), "digits": []}
        return jsonify(
            channel=channel_login(cfg, d),
            auto=d.get_meta("auto") or {"enabled": False},
            sync=d.get_meta("sync"),
            sync_error=worker.last_sync_error,
            needs_login=worker.needs_login,
            update={**{k: v for k, v in (d.get_meta("update") or {}).items() if k in ("available", "latest")},
                    "status": d.get_meta("update_status") or {}},
            tiktok=tiktok_state(),
            team=d.get_meta("team") or "",
            team_label_ready=(cfg.templates_dir / "rkm_label.png").exists(),
            poll_minutes=cfg.poll_minutes,
            calibrated=calibrated,
            pending_unknowns=len(d.pending_unknowns()),
            vods=[vod_json(r) for r in d.list_vods()],
        )

    @app.post("/api/vods/<vod_id>/generate")
    @login_required
    def api_generate(vod_id):
        row = db().get_vod(vod_id)
        if row is None:
            abort(404)
        if is_live_vod(db(), vod_id):
            return jsonify(error="Este VOD es del directo en curso; espera a que termine"), 409
        if row["status"] in ACTIVE:
            return jsonify(error="Ya se está procesando"), 409
        try:
            Templates.load(cfg.templates_dir, cfg.binarize)
        except Exception as exc:
            return jsonify(error=f"Falta calibrar el marcador: {exc}"), 409
        worker.enqueue(db(), vod_id)
        return jsonify(ok=True)

    @app.post("/api/vods/<vod_id>/cancel")
    @login_required
    def api_cancel(vod_id):
        return jsonify(ok=worker.cancel(db(), vod_id))

    @app.post("/api/sync")
    @login_required
    def api_sync():
        worker.sync_now()
        return jsonify(ok=True)

    @app.post("/api/auto")
    @login_required
    def api_auto():
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        state = set_auto(db(), enabled)
        if enabled:
            worker.sync_now()
        return jsonify(state)

    def clip_row(clip_id: int):
        row = db().get_clip(clip_id)
        if row is None:
            abort(404)
        return row

    @app.get("/media/clip/<int:clip_id>")
    @login_required
    def media_clip(clip_id):
        path = Path(clip_row(clip_id)["path"])
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True,
                         as_attachment=request.args.get("download") == "1", download_name=path.name)

    @app.get("/media/tiktok/<int:clip_id>")
    @login_required
    def media_tiktok(clip_id):
        row = clip_row(clip_id)
        path = Path(row["tiktok_path"]) if row["tiktok_path"] else None
        if row["tiktok_status"] != "ready" or path is None or not path.exists():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True,
                         as_attachment=request.args.get("download") == "1", download_name=path.name)

    @app.post("/api/clips/<int:clip_id>/tiktok")
    @login_required
    def api_tiktok(clip_id):
        """Crea (o rehace) la versión vertical de un clip."""
        row = clip_row(clip_id)
        if not Path(row["path"]).exists():
            return jsonify(error="El clip original ya no está en la carpeta"), 409
        worker.enqueue_render(db(), clip_id)
        return jsonify(ok=True)

    @app.post("/api/vods/<vod_id>/tiktok")
    @login_required
    def api_vod_tiktok(vod_id):
        """Crea la versión vertical de todos los clips del VOD que aún no la tienen."""
        if db().get_vod(vod_id) is None:
            abort(404)
        return jsonify(queued=worker.enqueue_renders_for_vod(db(), vod_id))

    def require_tiktok():
        """None si se puede enviar; si no, la respuesta de error."""
        if not cfg.tiktok_configured:
            return jsonify(error="Faltan TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en el .env"), 409
        if not tiktok_state()["connected"]:
            return jsonify(error="Conecta tu cuenta de TikTok primero"), 409
        return None

    @app.post("/api/clips/<int:clip_id>/tiktok/send")
    @login_required
    def api_tiktok_send(clip_id):
        """Envía la versión vertical de un clip como borrador a la cuenta de TikTok conectada."""
        row = clip_row(clip_id)
        if (refusal := require_tiktok()) is not None:
            return refusal
        if row["tiktok_status"] != "ready":
            return jsonify(error="Primero crea la versión vertical del clip"), 409
        if row["draft_status"] in ("queued", "uploading"):
            return jsonify(error="Ya se está enviando"), 409
        if tiktok.drafts_left(db()) == 0:
            return jsonify(error=tiktok_limit_message()), 409
        worker.enqueue_send(db(), clip_id)
        return jsonify(ok=True)

    @app.post("/api/vods/<vod_id>/tiktok/send")
    @login_required
    def api_vod_tiktok_send(vod_id):
        """Envía los clips listos del VOD, hasta el límite diario de TikTok."""
        if db().get_vod(vod_id) is None:
            abort(404)
        if (refusal := require_tiktok()) is not None:
            return refusal
        waiting = len(db().clips_ready_for_draft(vod_id))
        if waiting and tiktok.drafts_left(db()) == 0:
            return jsonify(error=tiktok_limit_message()), 409
        queued = worker.enqueue_sends_for_vod(db(), vod_id)
        return jsonify(queued=queued, remaining=waiting - queued)

    # --- compilaciones: varios goles en un solo vídeo / borrador ----------

    def comp_row(comp_id: int):
        row = db().get_compilation(comp_id)
        if row is None:
            abort(404)
        return row

    @app.post("/api/vods/<vod_id>/compilations")
    @login_required
    def api_compilation_create(vod_id):
        if db().get_vod(vod_id) is None:
            abort(404)
        try:
            ids = [int(i) for i in (request.get_json(silent=True) or {}).get("clip_ids", [])]
        except (TypeError, ValueError):
            return jsonify(error="Selección no válida"), 400
        try:
            comp_id = tiktok.make_compilation(cfg, db(), vod_id, ids)
        except (TikTokError, VerticalError) as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, id=comp_id)

    @app.get("/media/compilation/<int:comp_id>")
    @login_required
    def media_compilation(comp_id):
        path = Path(comp_row(comp_id)["path"])
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True,
                         as_attachment=request.args.get("download") == "1", download_name=path.name)

    @app.post("/api/compilations/<int:comp_id>/send")
    @login_required
    def api_compilation_send(comp_id):
        row = comp_row(comp_id)
        if (refusal := require_tiktok()) is not None:
            return refusal
        if not Path(row["path"]).exists():
            return jsonify(error="El vídeo ya no está en la carpeta; vuelve a juntar los goles"), 409
        if row["draft_status"] in ("queued", "uploading"):
            return jsonify(error="Ya se está enviando"), 409
        if tiktok.drafts_left(db()) == 0:
            return jsonify(error=tiktok_limit_message()), 409
        worker.enqueue_send_compilation(db(), comp_id)
        return jsonify(ok=True)

    @app.post("/api/compilations/<int:comp_id>/delete")
    @login_required
    def api_compilation_delete(comp_id):
        row = comp_row(comp_id)
        Path(row["path"]).unlink(missing_ok=True)
        if row["draft_status"] != "sent":
            db().delete_compilation(comp_id)  # una ya enviada se queda oculta: cuenta para el cupo diario
        return jsonify(ok=True)

    @app.get("/api/drafts/<kind>/<int:item_id>/status")
    @login_required
    def api_draft_status(kind, item_id):
        """Pregunta a TikTok en qué estado está un borrador enviado (para diagnosticar)."""
        row = comp_row(item_id) if kind == "comp" else clip_row(item_id) if kind == "clip" else abort(404)
        if not row["draft_publish_id"]:
            return jsonify(error="Este vídeo aún no se ha enviado a TikTok"), 409
        try:
            status, reason = tiktok.client_for(cfg, db()).draft_status(row["draft_publish_id"])
        except (TikTokError, NotConnected) as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(publish_id=row["draft_publish_id"], status=status, fail_reason=reason)

    def tiktok_limit_message() -> str:
        return (f"Ya enviaste {DRAFT_LIMIT} borradores en las últimas 24 h, el máximo que admite TikTok: "
                "publica o descarta alguno en la app de TikTok y vuelve a intentarlo mañana")

    @app.get("/media/thumb/<int:clip_id>")
    @login_required
    def media_thumb(clip_id):
        thumb = clip_row(clip_id)["thumb_path"]
        if not thumb or not Path(thumb).exists():
            abort(404)
        return send_file(thumb, mimetype="image/jpeg", max_age=3600)

    @app.post("/api/clips/<int:clip_id>/reveal")
    @login_required
    def api_reveal(clip_id):
        path = Path(clip_row(clip_id)["path"])
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        return jsonify(ok=True)

    @app.post("/api/open-clips-folder")
    @login_required
    def api_open_folder():
        cfg.clips_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(cfg.clips_dir)  # noqa: S606 — app local, carpeta propia
        return jsonify(ok=True)

    # --- calibración ---------------------------------------------------

    def calib_state() -> dict:
        d = db()
        ecfg = effective_config(cfg, d)
        digits_dir = cfg.templates_dir / "digits"
        return {
            "ref": [cfg.ref_width, cfg.ref_height],
            "roi": [ecfg.roi.x, ecfg.roi.y, ecfg.roi.w, ecfg.roi.h],
            "roi_saved": bool(d.get_meta(CALIB_ROI)),
            "number_box": d.get_meta(CALIB_NUMBER_BOX),
            "label": (cfg.templates_dir / "rkm_label.png").exists(),
            "digits": [d for d in range(10) if (digits_dir / f"{d}.png").exists()],
            "has_frame": calib_frame.exists(),
            "frame_info": d.get_meta(CALIB_FRAME),
            "team": d.get_meta("team") or "",
        }

    @app.get("/api/calibrate")
    @login_required
    def api_calib_state():
        return jsonify(calib_state())

    def load_frame(row, at: float, **extra):
        """Saca del stream (sin descargar el VOD) el fotograma de referencia para calibrar."""
        try:
            # Misma calidad que usa el análisis: las plantillas se recortan de imágenes iguales a
            # las que luego se leen, sea cual sea la calidad original del VOD.
            src = stream_url(row["url"], cfg.analysis_format)
            ffmpeg.grab_frame(cfg.ffmpeg, src, at, cfg.ref_width, cfg.ref_height, calib_frame)
        except Exception as exc:
            log.exception("No se pudo sacar el fotograma")
            return jsonify(error=f"No se pudo sacar el fotograma: {exc}"), 500
        db().set_meta(CALIB_FRAME, {"vod_id": row["id"], "at": format_ts(at), **extra})
        return jsonify(calib_state())

    def locate_label(frame: np.ndarray):
        """Posición de la etiqueta RKM en el fotograma completo, o None si no aparece."""
        label = imread_gray(cfg.templates_dir / "rkm_label.png")
        if label is None:
            return None
        roi = effective_config(cfg, db()).roi
        area = frame[roi.y:roi.y + roi.h, roi.x:roi.x + roi.w]
        res = cv2.matchTemplate(preprocess(area, cfg.binarize), preprocess(label, cfg.binarize),
                                cv2.TM_CCOEFF_NORMED)
        _, score, _, (lx, ly) = cv2.minMaxLoc(res)
        if score < cfg.label_threshold:
            return None
        return roi.x + lx, roi.y + ly

    @app.post("/api/calibrate/frame")
    @login_required
    def api_calib_frame():
        body = request.get_json(silent=True) or {}
        row = db().get_vod(str(body.get("vod_id", "")))
        if row is None:
            return jsonify(error="Elige un VOD"), 400
        return load_frame(row, float(body.get("at", 0)))

    @app.get("/media/calibration-frame")
    @login_required
    def media_calib_frame():
        if not calib_frame.exists():
            abort(404)
        return send_file(calib_frame, mimetype="image/png", max_age=0)

    @app.get("/media/template/<name>")
    @login_required
    def media_template(name):
        path = cfg.templates_dir / "rkm_label.png" if name == "label" else cfg.templates_dir / "digits" / f"{int(name)}.png"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/png", max_age=0)

    @app.post("/api/calibrate/save")
    @login_required
    def api_calib_save():
        body = request.get_json(silent=True) or {}
        kind = body.get("kind")
        try:
            x, y, w, h = (int(round(v)) for v in body["box"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Dibuja un recuadro primero"), 400
        if w < 3 or h < 3:
            return jsonify(error="El recuadro es demasiado pequeño"), 400
        d = db()

        if kind == "roi":
            d.set_meta(CALIB_ROI, [x, y, w, h])
            return jsonify(calib_state())

        frame = imread_gray(calib_frame)
        if frame is None:
            return jsonify(error="Carga antes un fotograma"), 400
        crop = frame[y:y + h, x:x + w]

        if kind == "label":
            imwrite(cfg.templates_dir / "rkm_label.png", trim_glyph(crop))
        elif kind == "digit":
            digit = int(body.get("digit", -1))
            if not 0 <= digit <= 9:
                return jsonify(error="Elige qué dígito es"), 400
            imwrite(cfg.templates_dir / "digits" / f"{digit}.png", trim_glyph(crop))
        elif kind == "number":
            # Se guarda relativo a la etiqueta, para que siga valiendo si el marcador se desplaza.
            if not (cfg.templates_dir / "rkm_label.png").exists():
                return jsonify(error="Marca antes la etiqueta RKM"), 400
            pos = locate_label(frame)
            if pos is None:
                return jsonify(error="No encuentro la etiqueta RKM dentro de la zona del marcador"), 400
            d.set_meta(CALIB_NUMBER_BOX, [x - pos[0], y - pos[1], w, h])
        else:
            return jsonify(error="Tipo de recuadro desconocido"), 400
        return jsonify(calib_state())

    @app.post("/api/calibrate/test")
    @login_required
    def api_calib_test():
        frame = imread_gray(calib_frame)
        if frame is None:
            return jsonify(error="Carga antes un fotograma"), 400
        d = db()
        ecfg = effective_config(cfg, d)
        try:
            reader = build_reader(ecfg, d)
        except Exception as exc:
            return jsonify(error=str(exc)), 400
        r = ecfg.roi
        reading = reader.read(frame[r.y:r.y + r.h, r.x:r.x + r.w])
        return jsonify(value=reading.value, label_score=round(reading.label_score, 3),
                       label_ok=reading.label_score >= ecfg.label_threshold)

    @app.post("/api/update")
    @login_required
    def api_update():
        d = db()
        if any(r["status"] in ACTIVE for r in d.list_vods()):
            return jsonify(error="Hay un VOD en proceso: actualiza cuando termine"), 409
        if (d.get_meta("update_status") or {}).get("state") == "running":
            return jsonify(error="Ya se está actualizando"), 409
        d.set_meta("update_status", {"state": "running"})

        def run():
            conn = Database(cfg.db_path)
            try:
                new_ref = updater.apply_update()
                conn.set_meta("update", {"available": False, "latest": new_ref, "current": new_ref,
                                         "checked_at": time.time()})
                conn.set_meta("update_status", {"state": "done"})
                log.info("Actualizado a %s; reiniciando", new_ref[:8])
                updater.restart_soon()
                time.sleep(1.5)  # deja que el panel lea el estado «done» antes de cerrar
                worker.stop()
                os._exit(0)
            except Exception as exc:
                log.exception("Actualización fallida")
                conn.set_meta("update_status", {"state": "error", "error": str(exc)})
            finally:
                conn.close()

        threading.Thread(target=run, name="rkm-update", daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/team")
    @login_required
    def api_team():
        name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:30]
        db().set_meta("team", name)
        return jsonify(team=name)

    # --- identidad del club: logo, color y estilo del marcador ---------------

    def club_json() -> dict:
        s = brand_mod.load_settings(db())
        pal = brand_mod.Palette(s.primary)
        return {
            "style": s.style, "primary": s.primary, "primary_auto": s.primary_auto,
            "short_name": s.short_name, "show_name": s.show_name,
            "team": db().get_meta("team") or "",
            "logo": {"url": f"/media/club/logo?h={s.logo_digest}", "width": s.logo_size[0], "height": s.logo_size[1],
                     "clean": s.logo_clean} if s.logo_digest else None,
            "suggested": list(s.suggested),
            "accent": brand_mod.rgb_hex(pal.accent),
            "design": tiktok.current_design(cfg, db()),
        }

    @app.get("/api/club")
    @login_required
    def api_club():
        return jsonify(club_json())

    @app.post("/api/club")
    @login_required
    def api_club_save():
        body = request.get_json(silent=True) or {}
        changes = {k: body[k] for k in ("style", "primary", "short_name", "show_name") if k in body}
        try:
            brand_mod.update_settings(db(), **changes)
        except brand_mod.BrandError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(club_json())

    @app.post("/api/club/logo")
    @login_required
    def api_club_logo():
        upload = request.files.get("logo")
        if upload is None:
            return jsonify(error="Elige un archivo de logo"), 400
        remove_bg = request.form.get("remove_bg", "1") not in ("0", "false", "")
        try:
            brand_mod.save_logo(cfg, db(), upload.read(brand_mod.MAX_UPLOAD_BYTES + 1), remove_bg)
        except brand_mod.BrandError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(club_json())

    @app.post("/api/club/logo/delete")
    @login_required
    def api_club_logo_delete():
        brand_mod.delete_logo(cfg, db())
        return jsonify(club_json())

    @app.get("/media/club/logo")
    @login_required
    def media_club_logo():
        path = brand_mod.logo_path(cfg)
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/png", max_age=3600)

    @app.get("/media/club/preview.png")
    @login_required
    def media_club_preview():
        """Vista previa del marcador con los ajustes guardados y, encima, los que aún no se han guardado."""
        d = db()
        s = brand_mod.load_settings(d)
        args = request.args
        try:
            primary = brand_mod.validate_color(args["primary"]) if "primary" in args else s.primary
        except brand_mod.BrandError:
            primary = s.primary
        show_name = args.get("show_name") in ("1", "true") if "show_name" in args else s.show_name
        short = " ".join(args.get("short_name", s.short_name).split())[:brand_mod.SHORT_NAME_MAX]
        try:
            old = max(0, min(int(args.get("old", 0)), 999))
            new = max(0, min(int(args.get("new", old + 1)), 999))
        except ValueError:
            old, new = 0, 1
        live = brand_mod.BrandSettings(**{**s.__dict__, "primary": primary, "show_name": show_name, "short_name": short})
        logo = None
        if s.logo_digest:
            try:
                from PIL import Image
                with Image.open(brand_mod.logo_path(cfg)) as f:
                    logo = f.convert("RGBA")
            except (OSError, ValueError):
                logo = None
        b = brand_mod.make_brand(live, d.get_meta("team") or "", logo)
        png = scoreboard_art.render_preview(b, old, new, channel_login(cfg, d) or "tu_canal", cfg.tiktok_tagline,
                                            width=min(max(int(args.get("w", 540)), 200), 1080),
                                            replay=args.get("replay") in ("1", "true"))
        resp = app.response_class(png, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # --- números sin reconocer -------------------------------------------

    @app.get("/api/unknowns")
    @login_required
    def api_unknowns():
        return jsonify([
            {"id": u["id"], "vod_id": u["vod_id"], "title": u["title"], "at": format_ts(u["at_s"])}
            for u in db().pending_unknowns()
        ])

    def unknown_row(unknown_id: int):
        row = db().get_unknown(unknown_id)
        if row is None or row["status"] != "pending":
            abort(404)
        return row

    @app.post("/api/unknowns/<int:unknown_id>/load")
    @login_required
    def api_unknown_load(unknown_id):
        u = unknown_row(unknown_id)
        # Un segundo después del inicio del tramo: el número ya está estable en pantalla.
        return load_frame(db().get_vod(u["vod_id"]), u["at_s"] + 1, unknown_id=unknown_id)

    @app.post("/api/unknowns/<int:unknown_id>/resolve")
    @login_required
    def api_unknown_resolve(unknown_id):
        unknown_row(unknown_id)
        d = db()
        digit = int((request.get_json(silent=True) or {}).get("digit", -1))
        if not 0 <= digit <= 9:
            return jsonify(error="Elige qué número es"), 400
        if (d.get_meta(CALIB_FRAME) or {}).get("unknown_id") != unknown_id:
            return jsonify(error="Carga antes el fotograma de este caso"), 400
        number_box = d.get_meta(CALIB_NUMBER_BOX)
        frame = imread_gray(calib_frame)
        pos = locate_label(frame) if frame is not None and number_box else None
        if pos is None:
            return jsonify(error="No encuentro «RKM» en este fotograma; descártalo o calibra a mano"), 400
        dx, dy, w, h = number_box
        x0, y0 = max(0, pos[0] + dx), max(0, pos[1] + dy)
        glyph = trim_glyph(frame[y0:y0 + h, x0:x0 + w])
        # Con dos cifras el recorte sale el doble de ancho: eso se marca a mano, cifra a cifra.
        try:
            known = Templates.load(cfg.templates_dir, cfg.binarize).digits
        except Exception:
            known = {}
        widths = sorted(t.shape[1] for t in known.values())
        if widths and glyph.shape[1] > 1.6 * widths[len(widths) // 2]:
            return jsonify(error="Parece un número de dos cifras: márcalo con «Un dígito», cifra a cifra"), 400
        imwrite(cfg.templates_dir / "digits" / f"{digit}.png", glyph)
        d.set_unknown_status(unknown_id, "resolved", digit)
        reader = build_reader(effective_config(cfg, d), d)
        r = effective_config(cfg, d).roi
        value = reader.read(frame[r.y:r.y + r.h, r.x:r.x + r.w]).value
        log.info("Dígito %s guardado desde el caso %s (lectura de comprobación: %s)", digit, unknown_id, value)
        return jsonify(ok=True, value=value, digits=calib_state()["digits"])

    @app.post("/api/unknowns/<int:unknown_id>/dismiss")
    @login_required
    def api_unknown_dismiss(unknown_id):
        unknown_row(unknown_id)
        db().set_unknown_status(unknown_id, "dismissed")
        return jsonify(ok=True)

    return app


def listen_addresses(host: str, port: int) -> str:
    """En Windows "localhost" suele resolver primero a ::1; si solo se escucha en IPv4,
    el navegador y el retorno del login de Twitch pueden fallar. Se escucha en ambas."""
    if host not in ("127.0.0.1", "localhost"):
        return f"{host}:{port}"
    addresses = [f"127.0.0.1:{port}"]
    if socket.has_ipv6:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                s.bind(("::1", 0))
            addresses.append(f"[::1]:{port}")
        except OSError:
            pass  # equipo sin IPv6: basta con IPv4
    return " ".join(addresses)


def serve(cfg: Config) -> None:
    from waitress import serve as waitress_serve

    db = Database(cfg.db_path)
    interrupted = db.reset_interrupted()
    if interrupted:
        log.info("%d VODs que estaban a medias quedan como fallidos (se pueden reintentar)", interrupted)
    db.reset_tiktok_interrupted()
    db.reset_draft_interrupted()
    db.close()

    worker = Worker(cfg)
    worker.start()
    app = create_app(cfg, worker)
    listen = listen_addresses(cfg.host, cfg.port)
    log.info("Panel en http://localhost:%d (escuchando en %s)", cfg.port, listen)
    try:
        waitress_serve(app, listen=listen, threads=8, _quiet=True)
    finally:
        worker.stop()
        time.sleep(0.2)
