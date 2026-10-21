"""Línea de comandos: python -m rkm_clips [serve|scan|snapshot|process|status|check]."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, ConfigError, load_config


def setup_logging(cfg: Config) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [
        RotatingFileHandler(cfg.logs_dir / "rkm_clips.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    ]
    if sys.stderr is not None:  # con pythonw.exe no hay consola
        handlers.append(logging.StreamHandler())
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("waitress").setLevel(logging.WARNING)


def parse_time(text: str) -> float:
    """Acepta segundos (754) o hh:mm:ss / mm:ss (00:12:34)."""
    seconds = 0.0
    for p in text.split(":"):
        seconds = seconds * 60 + float(p)
    return seconds


def cmd_serve(cfg: Config, args) -> int:
    from .pipeline import AlreadyRunning, single_instance
    from .web import serve

    try:
        with single_instance(cfg.data_dir / "rkm_clips.lock"):
            if args.open:
                webbrowser.open(f"http://localhost:{cfg.port}")
            serve(cfg)
    except AlreadyRunning:
        logging.getLogger("rkm_clips").info("La app ya está abierta en http://localhost:%d", cfg.port)
        if args.open:
            webbrowser.open(f"http://localhost:{cfg.port}")
    return 0


def cmd_update(cfg: Config, args) -> int:
    """Instala la última versión desde GitHub (el panel hace lo mismo con su botón)."""
    from . import updater
    from .db import Database

    db = Database(cfg.db_path)
    try:
        state = updater.check(db, force=True)
    finally:
        db.close()
    if state.get("error"):
        print(f"No se pudo consultar GitHub: {state['error']}")
        return 1
    if not state["available"]:
        print("Ya tienes la última versión.")
        return 0
    print("Instalando la actualización…")
    new_ref = updater.apply_update()
    print(f"Actualizado a {new_ref[:8]}. Cierra y abre de nuevo la app para usarla.")
    return 0


def cmd_process(cfg: Config, args) -> int:
    """Procesa un VOD concreto sin el panel (útil para depurar)."""
    from .db import Database
    from .pipeline import process_vod
    from .twitch import TwitchClient

    db = Database(cfg.db_path)
    try:
        vod = TwitchClient(cfg.client_id, token_store=db).get_video(args.vod_id)
        db.upsert_vod(vod)
        process_vod(cfg, db, vod.id)
        row = db.get_vod(vod.id)
        print(f"{vod.id}: {row['status']} · {row['clips']} clips" + (f" · {row['error']}" if row["error"] else ""))
        return 0 if row["status"] == "done" else 1
    finally:
        db.close()


def cmd_scan(cfg: Config, args) -> int:
    """Analiza un vídeo local con la calibración guardada; con --clips corta también los clips."""
    from .db import Database
    from .detector import format_ts
    from .pipeline import analyze_video, build_reader, cut_clips, effective_config

    db = Database(cfg.db_path)
    try:
        cfg = effective_config(cfg, db)
        video = Path(args.video)
        changes = analyze_video(cfg, video, build_reader(cfg, db))
        for c in changes:
            print(f"{format_ts(c.last_old_s)}  RKM {c.old} → {c.new}  (marcador actualizado en {format_ts(c.at_s)})")
        print(f"{len(changes)} subidas detectadas")
        if args.clips and changes:
            for p in cut_clips(cfg, video, video.stem, "local", changes):
                print(f"  {p}")
    finally:
        db.close()
    return 0


def cmd_snapshot(cfg: Config, args) -> int:
    """Guarda un fotograma de un vídeo local y prueba la lectura (alternativa a la calibración del panel)."""
    import cv2

    from .db import Database
    from .detector import imread_gray
    from .ffmpeg import grab_frame
    from .pipeline import build_reader, effective_config

    db = Database(cfg.db_path)
    try:
        cfg = effective_config(cfg, db)
        video = Path(args.video)
        at = parse_time(args.at)
        out = cfg.data_dir / "snapshots" / f"{video.stem}_{int(at)}s.png"
        grab_frame(cfg.ffmpeg, video, at, cfg.ref_width, cfg.ref_height, out)
        print(f"Fotograma: {out}")
        frame = imread_gray(out)
        r = cfg.roi
        reading = build_reader(cfg, db).read(frame[r.y:r.y + r.h, r.x:r.x + r.w])
        print(f"Etiqueta RKM: coincidencia {reading.label_score:.2f} · número leído: {reading.value}")
    finally:
        db.close()
    return 0


def cmd_status(cfg: Config, args) -> int:
    from .db import Database

    db = Database(cfg.db_path)
    try:
        rows = db.list_vods(args.limit)
        if not rows:
            print("Aún no hay VODs registrados")
        for r in rows:
            extra = f" · {r['error'][:80]}" if r["error"] else ""
            print(f"{r['created_at'][:16]}  {r['id']:>11}  {r['status']:<11} intentos={r['attempts']} "
                  f"clips={r['clips']}  {(r['title'] or '')[:50]}{extra}")
    finally:
        db.close()
    return 0


def cmd_check(cfg: Config, args) -> int:
    """Comprueba ffmpeg, plantillas y credenciales de Twitch."""
    ok = True
    ffmpeg = shutil.which(cfg.ffmpeg)
    print(f"ffmpeg: {ffmpeg or 'NO ENCONTRADO (instálalo o pon FFMPEG_PATH)'}")
    ok &= bool(ffmpeg)
    try:
        from .detector import Templates
        t = Templates.load(cfg.templates_dir, cfg.binarize)
        print(f"Plantillas: etiqueta {t.label.shape[1]}x{t.label.shape[0]}, dígitos {sorted(t.digits)}")
    except Exception as exc:
        print(f"Plantillas: aún sin calibrar ({exc})")
    try:
        from .db import Database
        from .pipeline import channel_login
        from .twitch import TwitchClient
        db = Database(cfg.db_path)
        login = channel_login(cfg, db)
        if not login:
            print("Twitch: aún no hay cuenta conectada (conéctala en el panel)")
            db.close()
            raise SystemExit(0 if ok else 1)
        client = TwitchClient(cfg.client_id, token_store=db)
        user_id = client.get_user_id(login)
        vods = client.get_archives(user_id, limit=5)
        live = client.get_live_stream_id(user_id)
        print(f"Twitch: canal {login} (id {user_id}), {'EN DIRECTO' if live else 'sin directo'}, "
              f"{len(vods)} VODs recientes")
        db.close()
    except Exception as exc:
        print(f"Twitch: ERROR {exc}")
        ok = False
    print(f"Panel: http://localhost:{cfg.port}  (redirect OAuth: {cfg.redirect_uri})")
    print(f"Datos: {cfg.data_dir}\nClips: {cfg.clips_dir}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rkm_clips", description="Clips automáticos de los goles de RKM en tus VODs de Twitch")
    parser.add_argument("--env", type=Path, help="archivo .env alternativo")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="abrir el panel web (por defecto)")
    p.add_argument("--open", action="store_true", help="abrir el navegador al arrancar")
    p = sub.add_parser("process", help="procesar un VOD concreto por su id, sin panel")
    p.add_argument("vod_id")
    p = sub.add_parser("scan", help="analizar un vídeo local")
    p.add_argument("video")
    p.add_argument("--clips", action="store_true", help="cortar también los clips")
    p = sub.add_parser("snapshot", help="sacar un fotograma de un vídeo local y probar la lectura")
    p.add_argument("video")
    p.add_argument("--at", required=True, help="instante: segundos o hh:mm:ss")
    p = sub.add_parser("status", help="últimos VODs registrados")
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("check", help="comprobar ffmpeg, plantillas y Twitch")
    sub.add_parser("update", help="instalar la última versión desde GitHub")

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]) + ["serve", "--open"])
    try:
        cfg = load_config(args.env)
    except ConfigError as exc:
        print(f"Configuración: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)

    # Calibración base y migración de versiones anteriores (idempotente: no pisa nada del usuario).
    from .bootstrap import prepare_user_data
    from .db import Database
    boot_db = Database(cfg.db_path)
    try:
        prepare_user_data(cfg, boot_db)
    finally:
        boot_db.close()

    handlers = {
        "serve": cmd_serve, "process": cmd_process, "scan": cmd_scan,
        "snapshot": cmd_snapshot, "status": cmd_status, "check": cmd_check, "update": cmd_update,
    }
    try:
        return handlers[args.command](cfg, args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        logging.getLogger("rkm_clips").exception("Error en %s", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
