import re
import shutil
import sqlite3
import subprocess

import numpy as np
import pytest

from rkm_clips import vertical
from rkm_clips.db import Database
from rkm_clips.vertical import (
    VerticalOptions, camera_keys, make_caption, piecewise_expr, probe, render_vertical, smooth_path, track_action,
)

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe no están en el PATH")


def eval_expr(expr: str, t: float) -> float:
    """Evalúa una expresión de ffmpeg de `piecewise_expr` con la sintaxis de Python."""
    py = re.sub(r"\bif\(", "iff(", expr)
    return eval(py, {"iff": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "t": t})


def test_piecewise_expr_interpolates_between_keys():
    keys = [(0.0, 100), (1.0, 300), (2.0, 300), (3.0, 0)]
    expr = piecewise_expr(keys)
    for t, want in [(0, 100), (0.5, 200), (1, 300), (1.5, 300), (2.5, 150), (3, 0), (9, 0)]:
        assert eval_expr(expr, t) == pytest.approx(want)
    assert piecewise_expr([(0.0, 42)]) == "42"


def test_camera_keys_stay_inside_the_frame_and_are_relative_to_the_window():
    path = [(i / 10, 0.02 if i < 20 else 0.99) for i in range(40)]   # de un extremo al otro
    keys = camera_keys(path, src_w=1280, crop_w=720, start=1.0, end=3.0)
    assert keys[0][0] == 0.0 and keys[-1][0] == pytest.approx(2.0)
    assert all(0 <= x <= 1280 - 720 for _, x in keys)
    assert keys[0][1] == 0 and keys[-1][1] == 1280 - 720


def test_smooth_path_removes_jitter():
    rng = np.random.default_rng(1)
    raw = [(i / 10, 0.5 + float(rng.normal(0, 0.1))) for i in range(100)]
    smooth = smooth_path(raw)
    assert np.std([c for _, c in smooth]) < np.std([c for _, c in raw]) / 2


def test_caption_has_team_score_channel_and_hashtags():
    text = make_caption("RKM", 3, "kangrykelvin", "#EAFC #ProClubs")
    assert text == "¡GOOOL! ⚽ Gol 3 de RKM · en directo en twitch.tv/kangrykelvin #EAFC #ProClubs"
    assert make_caption("", 1, "", "#x") == "¡GOOOL! ⚽ Gol 1 #x"


def make_video(path, seconds=10, fps=30, moving=True, audio=True):
    """1280×720: fondo gris con un cuadrado blanco que cruza de izquierda a derecha."""
    w, h = 1280, 720
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    proc = subprocess.Popen(cmd + [str(path)], stdin=subprocess.PIPE)
    for i in range(seconds * fps):
        frame = np.full((h, w, 3), 90, np.uint8)
        x = int(100 + (w - 300) * i / (seconds * fps)) if moving else 600
        frame[300:420, x:x + 120] = 255
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0


@needs_ffmpeg
def test_track_action_follows_a_moving_object(tmp_path):
    video = tmp_path / "mueve.mp4"
    make_video(video, audio=False)
    path = smooth_path(track_action("ffmpeg", video))
    first, last = path[5][1], path[-5][1]
    assert first < 0.3 and last > 0.7 and last - first > 0.4


@needs_ffmpeg
def test_render_vertical_makes_a_9_16_video_with_replay_and_audio(tmp_path):
    clip = tmp_path / "sintético ñ.mp4"   # ruta con caracteres no ASCII a propósito
    make_video(clip, seconds=10)
    out = render_vertical("ffmpeg", clip, tmp_path / "salida" / "v.mp4", goal_s=7.0, old=1, new=2,
                          team="RKM", channel="kangrykelvin",
                          opts=VerticalOptions(lead_s=3.0, replay_before_s=2.0, replay_after_s=1.0, replay_slow=0.5))
    p = probe("ffmpeg", out)
    assert (p.width, p.height) == (1080, 1920) and p.has_audio
    # jugada: 10 − (7−3) = 6 s; repetición: 3 s a mitad de velocidad = 6 s
    assert p.duration == pytest.approx(12.0, abs=0.8)
    assert not list((tmp_path / "salida").glob("*.parte.mp4"))   # no quedan temporales


@needs_ffmpeg
def test_render_vertical_works_without_audio_and_when_goal_is_at_the_start(tmp_path):
    clip = tmp_path / "mudo.mp4"
    make_video(clip, seconds=6, audio=False)
    out = render_vertical("ffmpeg", clip, tmp_path / "v.mp4", goal_s=0.5, old=0, new=1,
                          opts=VerticalOptions(lead_s=10.0, replay_before_s=4.5, replay_after_s=1.0))
    p = probe("ffmpeg", out)
    assert (p.width, p.height) == (1080, 1920) and not p.has_audio


@needs_ffmpeg
def test_render_failure_raises_and_leaves_no_partial_file(tmp_path):
    bad = tmp_path / "roto.mp4"
    bad.write_bytes(b"esto no es un video")
    with pytest.raises(vertical.VerticalError):
        render_vertical("ffmpeg", bad, tmp_path / "v.mp4", goal_s=1, old=0, new=1)
    assert not (tmp_path / "v.mp4").exists()


def test_database_gains_tiktok_columns_when_opening_an_old_file(tmp_path):
    path = tmp_path / "viejo.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY AUTOINCREMENT, vod_id TEXT, change_s REAL, "
                 "start_s REAL, end_s REAL, old_value INT, new_value INT, path TEXT, thumb_path TEXT, created_at TEXT)")
    conn.execute("INSERT INTO clips (vod_id, change_s, start_s, end_s, old_value, new_value, path, created_at) "
                 "VALUES ('1', 15, 0, 18, 0, 1, 'x.mp4', 'ya')")
    conn.commit()
    conn.close()
    db = Database(path)
    row = db.conn.execute("SELECT * FROM clips").fetchone()
    assert row["tiktok_status"] == "" and row["tiktok_path"] is None
    Database(path).close()   # abrirla otra vez no falla por columnas repetidas
