import shutil
from pathlib import Path
import subprocess
import time
from dataclasses import replace

import pytest

from conftest import scoreboard
from rkm_clips.config import Roi, load_config
from rkm_clips.db import Database
from rkm_clips.detector import Change
from rkm_clips.pipeline import (
    AlreadyRunning, analyze_video, build_reader, clip_name, clip_window, cut_clips, is_live_vod,
    single_instance, sync_vods,
)
from rkm_clips.twitch import Vod, parse_duration, thumbnail
from rkm_clips.worker import set_auto


def vod(i: int, stream_id: str | None = None) -> Vod:
    return Vod(id=str(i), stream_id=stream_id or f"s{i}", title=f"VOD {i}",
               created_at=f"2026-10-{i:02d}T20:00:00Z", url=f"https://www.twitch.tv/videos/{i}",
               duration_s=3600, thumbnail_url="https://x/%{width}x%{height}.jpg")


class FakeTwitch:
    def __init__(self, vods, live=None):
        self.vods, self.live = vods, live

    def get_user_id(self, login):
        return "42"

    def get_archives(self, user_id, limit=20):
        return self.vods

    def get_live_stream_id(self, user_id):
        return self.live


@pytest.fixture
def cfg(tmp_path, template_dir, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    monkeypatch.setenv("TEMPLATES_DIR", str(template_dir))
    return load_config(env_file=tmp_path / "no.env")


def test_parse_duration_and_thumbnail():
    assert parse_duration("3h2m10s") == 10930
    assert parse_duration("45m3s") == 2703
    assert parse_duration("12s") == 12
    assert parse_duration("") == 0
    assert thumbnail("https://x/%{width}x%{height}.jpg", 320, 180) == "https://x/320x180.jpg"


def test_clip_window_anchors():
    change = Change(at_s=3750.0, old=4, new=5, last_old_s=3725.0)
    assert clip_window(change, 15, 0) == (3710.0, 3725.0)
    assert clip_window(change, 15, 1, anchor="first_new") == (3735.0, 3751.0)
    assert clip_window(Change(9.0, 0, 1, 5.0), 15, 1) == (0.0, 6.0)
    assert clip_name("2271", "2026-10-20T19:00:00Z", change) == "20261020_2271_01-02-05_RKM4-5.mp4"


def test_sync_marks_new_and_live_vods(cfg):
    db = Database(":memory:")
    twitch = FakeTwitch([vod(2, stream_id="live"), vod(1)], live="live")
    sync_vods(cfg, db, twitch, "kangrykelvin")
    assert [r["id"] for r in db.list_vods()] == ["2", "1"]
    assert db.get_vod("1")["status"] == "new"
    assert is_live_vod(db, "2") and not is_live_vod(db, "1")
    twitch.live = None
    sync_vods(cfg, db, twitch, "kangrykelvin")
    assert not is_live_vod(db, "2")


def test_auto_mode_only_takes_vods_seen_after_enabling(cfg):
    db = Database(":memory:")
    sync_vods(cfg, db, FakeTwitch([vod(1)]), "k")
    auto = set_auto(db, True)
    assert db.auto_candidates(auto["since"], cfg.max_attempts) == []
    time.sleep(0.02)  # la resolución del reloj en Windows es de ~15 ms
    sync_vods(cfg, db, FakeTwitch([vod(2), vod(1)]), "k")
    assert [r["id"] for r in db.auto_candidates(auto["since"], cfg.max_attempts)] == ["2"]
    # Reactivar no mueve la fecha si ya estaba activo.
    assert set_auto(db, True)["since"] == auto["since"]


def test_failed_vod_retried_until_max_attempts(cfg):
    db = Database(":memory:")
    since = set_auto(db, True)["since"] - 1
    db.upsert_vod(vod(2))
    for _ in range(cfg.max_attempts):
        assert db.auto_candidates(since, cfg.max_attempts)
        db.start_attempt("2")
        db.set_status("2", "failed", error="boom")
    assert db.auto_candidates(since, cfg.max_attempts) == []


def test_interrupted_work_becomes_failed():
    db = Database(":memory:")
    db.upsert_vod(vod(1))
    db.start_attempt("1")
    assert db.reset_interrupted() == 1
    assert db.get_vod("1")["status"] == "failed"


def test_user_token_roundtrip_and_logout(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    assert db.get_user_token() is None
    db.save_user_token("acc", "ref", expires_at=123.0)
    db.set_meta("user", {"id": "1"})
    assert db.get_user_token() == {"access": "acc", "refresh": "ref", "expires_at": 123.0}
    db.clear_user_token()
    assert db.get_user_token() is None and db.get_meta("user") is None


def test_single_instance_lock(tmp_path):
    lock = tmp_path / "x.lock"
    with single_instance(lock):
        with pytest.raises(AlreadyRunning):
            with single_instance(lock):
                pass
    with single_instance(lock):  # liberado al salir
        pass


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg no está en el PATH")
def test_end_to_end_on_synthetic_video(cfg, templates, tmp_path):
    """40 s a 640x360: RKM 3 hasta t=18, marcador oculto (celebración) hasta t=24 con 4,
    y 4→5 en t=32 sin ocultarse."""
    w, h, fps = 640, 360, 4
    cfg = replace(cfg, ref_width=w, ref_height=h, roi=Roi(0, 0, 360, 240), digits_width=200,
                  clip_before=15, clip_after=1)
    video = tmp_path / "sintético.mp4"  # ruta no ASCII a propósito
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(video)],
        stdin=subprocess.PIPE,
    )
    for i in range(40 * fps):
        t = i / fps
        value = 3 if t < 18 else None if t < 24 else 4 if t < 32 else 5
        frame = scoreboard(None, templates, size=(h, w))
        frame[:240, :360] = scoreboard(value, templates)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0

    changes = analyze_video(cfg, video, build_reader(cfg))
    assert [(c.old, c.new, c.last_old_s, c.at_s) for c in changes] == [(3, 4, 17.0, 24.0), (4, 5, 31.0, 32.0)]

    db = Database(":memory:")
    clips = cut_clips(cfg, video, "test", "2026-10-21", changes, db)
    # En la BD queda el momento del gol (último segundo con el marcador anterior), como en el nombre.
    assert [r["change_s"] for r in db.clips_for("test")] == [17.0, 31.0]
    assert [p.name for p in clips] == ["20261021_test_00-00-17_RKM3-4.mp4", "20261021_test_00-00-31_RKM4-5.mp4"]
    for p in clips:
        assert p.with_suffix(".jpg").exists()
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        assert abs(float(out.stdout) - 16) < 0.6


def test_unknowns_are_capped_per_vod():
    from rkm_clips.db import MAX_UNKNOWNS_PER_VOD
    db = Database(":memory:")
    db.upsert_vod(vod(1))
    added = sum(db.add_unknown("1", float(t)) for t in range(MAX_UNKNOWNS_PER_VOD + 5))
    assert added == MAX_UNKNOWNS_PER_VOD
    assert db.unknowns_for("1") == MAX_UNKNOWNS_PER_VOD
    db.clear_unknowns("1")
    assert db.unknowns_for("1") == 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg no está en el PATH")
def test_process_vod_end_to_end_with_stream_fallback(cfg, templates, tmp_path, monkeypatch):
    """process_vod completo: descarga (simulada) de la copia de análisis, detección, número
    sin plantilla registrado y, si el stream no responde, clips cortados de la copia local."""
    import rkm_clips.pipeline as pipeline
    w, h, fps = 640, 360, 2
    cfg = replace(cfg, ref_width=w, ref_height=h, roi=Roi(0, 0, 360, 240), digits_width=200,
                  clip_before=5, clip_after=1)
    (cfg.templates_dir / "digits" / "9.png").unlink()  # el 9 queda sin plantilla
    video = tmp_path / "analisis.mp4"
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(video)], stdin=subprocess.PIPE)
    for i in range(30 * fps):
        t = i / fps
        frame = scoreboard(None, templates, size=(h, w))
        frame[:240, :360] = scoreboard(1 if t < 10 else 2 if t < 20 else 9, templates)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0

    seen = {}
    def fake_download(url, vod_id, dest, fmt, *a, **k):
        seen["fmt"] = fmt
        return video
    def broken_stream(url, fmt):
        raise RuntimeError("sin red")
    monkeypatch.setattr(pipeline, "download_vod", fake_download)
    monkeypatch.setattr(pipeline, "stream_url", broken_stream)

    db = Database(cfg.db_path)
    db.upsert_vod(vod(7))
    pipeline.process_vod(replace(cfg, keep_vods=True), db, "7")

    row = db.get_vod("7")
    assert row["status"] == "done", row["error"]
    assert seen["fmt"] == cfg.analysis_format
    assert [(c["old_value"], c["new_value"]) for c in db.clips_for("7")] == [(1, 2)]
    assert all(Path(c["path"]).exists() for c in db.clips_for("7"))
    assert db.unknowns_for("7") == 1  # el tramo con el 9


def test_progress_can_be_reported_from_download_threads(tmp_path):
    """yt-dlp avisa del progreso desde sus hilos de fragmentos (fallo real visto con un VOD)."""
    import threading as th
    from rkm_clips.pipeline import _Progress
    db = Database(tmp_path / "p.sqlite3")
    db.upsert_vod(vod(1))
    progress = _Progress(db, "1", None)
    errors = []
    def worker():
        try:
            progress(0.42)
        except Exception as exc:  # pragma: no cover - es justo lo que no debe pasar
            errors.append(exc)
    t = th.Thread(target=worker); t.start(); t.join()
    assert errors == []
    assert db.get_vod("1")["progress"] == 0.42
