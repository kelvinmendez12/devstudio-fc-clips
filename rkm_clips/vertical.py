"""Versión vertical (9:16) de un clip para TikTok, sin edición manual.

Una sola pasada de ffmpeg hace todo: fondo difuminado, recorte cuadrado que sigue la jugada,
textos, repetición a cámara lenta y audio. Python solo calcula hacia dónde mirar (movimiento
sobre imágenes diminutas) y dibuja los textos.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

W, H = 1080, 1920
BOX = 1080            # el recorte de la jugada ocupa todo el ancho (cuadrado)
BOX_Y = 560           # deja sitio arriba para el título y abajo para el canal
ACCENT = (145, 70, 255)
ACCENT_HEX = "0xFF4691"
OUT_FPS = 30
TRACK_FPS = 10
TRACK_W, TRACK_H = 320, 180
KEY_EVERY_S = 0.25    # cada cuánto se fija un punto de la cámara virtual

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FONTS_TITLE = [r"C:\Windows\Fonts\impact.ttf", "impact.ttf", "DejaVuSans-Bold.ttf"]
FONTS_TEXT = [r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\arialbd.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]


class VerticalError(Exception):
    pass


@dataclass(frozen=True)
class VerticalOptions:
    lead_s: float = 10.0        # segundos de jugada antes del gol
    replay_before_s: float = 4.5
    replay_after_s: float = 1.0
    replay_slow: float = 0.5    # 0,5 = mitad de velocidad
    tagline: str = "EA FC · Clubes Pro"


@dataclass(frozen=True)
class Probe:
    width: int
    height: int
    duration: float
    has_audio: bool


# --- ffprobe ---------------------------------------------------------------------------------

def _ffprobe_path(ffmpeg: str) -> str:
    p = Path(ffmpeg)
    if p.name.lower().startswith("ffmpeg"):
        return str(p.with_name(p.name.lower().replace("ffmpeg", "ffprobe", 1)) if p.parent != Path(".") else "ffprobe")
    return "ffprobe"


def probe(ffmpeg: str, video: Path) -> Probe:
    out = subprocess.run(
        [_ffprobe_path(ffmpeg), "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=60,
    )
    if out.returncode != 0:
        raise VerticalError(f"No se pudo leer el clip: {out.stderr.strip()[-200:]}")
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise VerticalError("El clip no tiene vídeo")
    return Probe(int(v["width"]), int(v["height"]), float(info["format"]["duration"]),
                 any(s.get("codec_type") == "audio" for s in streams))


# --- seguimiento de la jugada --------------------------------------------------------------

def iter_small_frames(ffmpeg: str, video: Path, fps: int = TRACK_FPS):
    """(segundo, imagen en gris de TRACK_W×TRACK_H) a `fps` por segundo."""
    proc = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(video), "-an", "-sn",
         "-vf", f"fps={fps},scale={TRACK_W}:{TRACK_H}:flags=area,format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW,
    )
    size = TRACK_W * TRACK_H
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(TRACK_H, TRACK_W)
            i += 1
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()
        proc.wait()


def track_action(ffmpeg: str, video: Path, fps: int = TRACK_FPS) -> list[tuple[float, float]]:
    """Centro horizontal de la acción, en fracción del ancho (0–1), por muestra.

    Es la media de dónde hay movimiento entre fotogramas seguidos, sin mirar el marcador de arriba
    ni los nombres de abajo. Si no hay movimiento suficiente se mantiene el último valor.
    """
    import cv2

    centers: list[float | None] = []
    times: list[float] = []
    prev = None
    for t, g in iter_small_frames(ffmpeg, video, fps):
        g = cv2.GaussianBlur(g, (5, 5), 0)
        c = None
        if prev is not None:
            diff = cv2.absdiff(g, prev)
            diff[: int(TRACK_H * 0.16)] = 0
            diff[int(TRACK_H * 0.82):] = 0
            diff[diff < 20] = 0
            col = diff.sum(axis=0).astype(np.float64)
            if col.sum() > 1500:
                c = float((col * np.arange(TRACK_W)).sum() / col.sum()) / TRACK_W
        prev = g
        centers.append(c)
        times.append(t)
    if not times:
        raise VerticalError("No se pudo analizar el movimiento del clip")
    # Se rellenan los huecos con el último valor conocido (y el primero hacia atrás).
    first = next((c for c in centers if c is not None), 0.5)
    last = first
    filled = []
    for c in centers:
        last = c if c is not None else last
        filled.append(last)
    return list(zip(times, filled))


def smooth_path(samples: list[tuple[float, float]], window_s: float = 1.6, fps: int = TRACK_FPS) -> list[tuple[float, float]]:
    """Media móvil: la cámara virtual no debe dar tirones."""
    xs = np.array([c for _, c in samples])
    k = max(1, int(window_s * fps) // 2)
    padded = np.pad(xs, k, mode="edge")
    smooth = np.convolve(padded, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    return [(t, float(c)) for (t, _), c in zip(samples, smooth)]


def camera_keys(path: list[tuple[float, float]], src_w: int, crop_w: int, start: float, end: float) -> list[tuple[float, int]]:
    """Posición x (en píxeles del original) del recorte cada KEY_EVERY_S entre start y end.

    Los tiempos salen relativos a `start` (el tramo se recorta y se reinicia a 0 antes de recortar).
    """
    ts = np.array([t for t, _ in path])
    cs = np.array([c for _, c in path])
    keys = []
    n = max(1, int(round((end - start) / KEY_EVERY_S)))
    for i in range(n + 1):
        t = min(start + i * KEY_EVERY_S, end)
        center = float(np.interp(t, ts, cs)) * src_w
        x = int(round(min(max(center - crop_w / 2, 0), src_w - crop_w)))
        keys.append((round(t - start, 3), x))
    return keys


def piecewise_expr(keys: list[tuple[float, int]]) -> str:
    """Expresión de ffmpeg en `t` que interpola linealmente entre los puntos (para `crop=x=`)."""
    if len(keys) == 1:
        return str(keys[0][1])
    expr = str(keys[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        if t1 <= t0:
            continue
        if x0 == x1:
            seg = str(x0)
        else:
            seg = f"{x0}+({x1 - x0})*(t-{t0})/{round(t1 - t0, 3)}"
        expr = f"if(lt(t,{t1}),{seg},{expr})"
    return expr


# --- textos --------------------------------------------------------------------------------------

def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def text_layer(team: str, old: int, new: int, channel: str, tagline: str, replay: bool) -> Image.Image:
    """Capa RGBA 1080×1920 con todo el texto fijo."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    big, mid, small = _font(FONTS_TITLE, 150), _font(FONTS_TEXT, 64), _font(FONTS_TEXT, 40)

    def centered(y, txt, font, fill, stroke):
        bw = d.textlength(txt, font=font)
        d.text(((W - bw) / 2, y), txt, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))

    centered(150, "¡GOOOL!", big, (255, 255, 255, 255), 10)
    score = f"{team.upper() or 'GOL'}  {old}  →  {new}"
    sw = d.textlength(score, font=mid)
    py = 335
    d.rounded_rectangle(((W - sw) / 2 - 36, py - 14, (W + sw) / 2 + 36, py + 84), 26, fill=ACCENT + (255,))
    centered(py, score, mid, (255, 255, 255, 255), 0)
    if channel:
        centered(H - 235, f"twitch.tv/{channel}", small, (255, 255, 255, 235), 4)
    if tagline:
        centered(H - 175, tagline, small, (210, 210, 220, 210), 4)
    if replay:
        tag = _font(FONTS_TEXT, 50)
        label = "REPETICIÓN"
        lw = d.textlength(label, font=tag)
        d.rounded_rectangle((40, BOX_Y + 40, 40 + lw + 60, BOX_Y + 130), 20, fill=(230, 40, 60, 255))
        d.text((70, BOX_Y + 52), label, font=tag, fill=(255, 255, 255, 255))
    return img


def make_caption(team: str, new: int, channel: str, hashtags: str) -> str:
    """Texto sugerido para el post: lo que se ve antes de «más» son ~100–150 caracteres."""
    of = f" de {team}" if team else ""
    live = f" · en directo en twitch.tv/{channel}" if channel else ""
    return f"¡GOOOL! ⚽ Gol {new}{of}{live} {hashtags}".strip()


# --- ffmpeg ------------------------------------------------------------------------------------

def build_filter(src: Probe, a_keys, b_keys, a_win, b_win, opts: VerticalOptions, with_audio: bool) -> str:
    crop = min(src.height, src.width)
    a0, a1 = a_win
    b0, b1 = b_win
    slow = 1.0 / opts.replay_slow

    def segment(label_in: str, win, keys, overlay_idx: int, tag: str) -> str:
        start, end = win
        return (
            f"[{label_in}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,fps={OUT_FPS},split=2[{tag}b][{tag}m];"
            f"[{tag}b]scale=135:240:force_original_aspect_ratio=increase,crop=135:240,boxblur=2:1,"
            f"scale={W}:{H}:flags=bicubic,eq=brightness=-0.18:saturation=0.9[{tag}bg];"
            f"[{tag}m]crop=w={crop}:h={crop}:x='{piecewise_expr(keys)}':y=0,scale={BOX}:{BOX}:flags=lanczos[{tag}mm];"
            f"[{tag}bg][{tag}mm]overlay=0:{BOX_Y},"
            f"drawbox=x=0:y={BOX_Y - 6}:w={W}:h=6:color={ACCENT_HEX}:t=fill,"
            f"drawbox=x=0:y={BOX_Y + BOX}:w={W}:h=5:color={ACCENT_HEX}:t=fill[{tag}c];"
            f"[{tag}c][{overlay_idx}:v]overlay=0:0:format=auto:shortest=1[{tag}v]"
        )

    graph = [
        "[0:v]split=2[va][vr]",
        segment("va", (a0, a1), a_keys, 1, "a"),
        segment("vr", (b0, b1), b_keys, 2, "b"),
        f"[bv]setpts={slow:.3f}*PTS[bs]",
        f"[av][bs]concat=n=2:v=1:a=0[vc]",
        f"[vc]fps={OUT_FPS},format=yuv420p[v]",
    ]
    if with_audio:
        graph += [
            "[0:a]asplit=2[a0][a1]",
            f"[a0]atrim=start={a0:.3f}:end={a1:.3f},asetpts=PTS-STARTPTS[aA]",
            f"[a1]atrim=start={b0:.3f}:end={b1:.3f},asetpts=PTS-STARTPTS,atempo={opts.replay_slow:.3f}[aB]",
            "[aA][aB]concat=n=2:v=0:a=1[a]",
        ]
    return ";".join(graph)


def render_vertical(ffmpeg: str, clip: Path, out: Path, *, goal_s: float, old: int, new: int, team: str = "",
                    channel: str = "", opts: VerticalOptions | None = None) -> Path:
    """Genera la versión vertical de `clip`. `goal_s` es el segundo del gol dentro del clip."""
    opts = opts or VerticalOptions()
    src = probe(ffmpeg, clip)
    goal = min(max(goal_s, 0.0), src.duration)
    a_win = (max(0.0, goal - opts.lead_s), src.duration)
    b_win = (max(0.0, goal - opts.replay_before_s), min(src.duration, goal + opts.replay_after_s))
    if b_win[1] - b_win[0] < 1.0:
        b_win = (max(0.0, src.duration - 3.0), src.duration)

    crop = min(src.height, src.width)
    path = smooth_path(track_action(ffmpeg, clip))
    a_keys = camera_keys(path, src.width, crop, *a_win)
    b_keys = camera_keys(path, src.width, crop, *b_win)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(out.stem + ".parte" + out.suffix)
    with tempfile.TemporaryDirectory() as tmp:
        pa, pb = Path(tmp) / "a.png", Path(tmp) / "b.png"
        text_layer(team, old, new, channel, opts.tagline, replay=False).save(pa)
        text_layer(team, old, new, channel, opts.tagline, replay=True).save(pb)
        graph = build_filter(src, a_keys, b_keys, a_win, b_win, opts, src.has_audio)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(clip),
               "-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{a_win[1] - a_win[0] + 1:.3f}", "-i", str(pa),
               "-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{b_win[1] - b_win[0] + 1:.3f}", "-i", str(pb),
               "-filter_complex", graph, "-map", "[v]"]
        if src.has_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(tmp_out)]
        result = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=600)
    if result.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        raise VerticalError(f"ffmpeg no pudo crear la versión vertical: {result.stderr.decode(errors='replace')[-400:]}")
    os.replace(tmp_out, out)
    return out
