"""Llamadas a ffmpeg: lectura de la zona del marcador a 1 fps y corte de clips."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .config import Roi

log = logging.getLogger(__name__)

# Sin ventana de consola cuando corre desde el Programador de tareas con pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def crop_filter(ref_width: int, ref_height: int, roi: Roi, fps: float) -> str:
    # Se escala primero a la resolución de referencia para que las plantillas
    # sirvan aunque el VOD se haya descargado a otra calidad.
    return (
        f"fps={fps},scale={ref_width}:{ref_height}:flags=area,"
        f"crop={roi.w}:{roi.h}:{roi.x}:{roi.y},format=gray"
    )


def iter_roi_frames(
    ffmpeg: str,
    video: Path,
    ref_width: int,
    ref_height: int,
    roi: Roi,
    fps: float = 1.0,
    hwaccel: str = "",
) -> Iterator[tuple[float, np.ndarray]]:
    """Devuelve (segundo, recorte en escala de grises) para cada fotograma muestreado."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += [
        "-i", str(video),
        "-an", "-sn",
        "-vf", crop_filter(ref_width, ref_height, roi, fps),
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    frame_size = roi.w * roi.h
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW,
        bufsize=frame_size * 4,
    )
    index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            yield index / fps, np.frombuffer(buf, dtype=np.uint8).reshape(roi.h, roi.w)
            index += 1
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        stderr = proc.stderr.read().decode(errors="replace").strip()
        proc.stderr.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg falló al leer {video.name} (código {code}): {stderr[-500:]}")


def _input_opts(video: Path | str) -> list[str]:
    # Al leer el stream de Twitch por HTTP, reintentar si se corta la conexión a mitad.
    if str(video).startswith(("http://", "https://")):
        return ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    return []


def grab_frame(ffmpeg: str, video: Path | str, at_s: float, ref_width: int, ref_height: int, out: Path) -> None:
    """Guarda un fotograma completo (a resolución de referencia) para calibrar ROI y plantillas."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            *_input_opts(video), "-ss", f"{at_s:.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={ref_width}:{ref_height}:flags=area",
            str(out),
        ],
        check=True, capture_output=True, creationflags=_NO_WINDOW,
    )


def cut_clip(ffmpeg: str, video: Path | str, start_s: float, end_s: float, out: Path, reencode: bool = True) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *_input_opts(video), "-ss", f"{start_s:.3f}", "-i", str(video), "-t", f"{end_s - start_s:.3f}",
    ]
    if reencode:
        # Re-codificar da cortes exactos al segundo; con copia el inicio salta al keyframe anterior.
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    cmd.append(str(out))
    result = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg no pudo cortar {out.name}: {result.stderr.decode(errors='replace')[-500:]}")
