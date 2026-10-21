"""Lectura del número de la fila "RKM" por template matching y detección de subidas.

Plantillas (en TEMPLATES_DIR, recortadas del recorte ROI a resolución de referencia):
  rkm_label.png      la palabra "RKM" del marcador
  digits/0.png …     un PNG por dígito, tal como aparece en el marcador
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


def imread_gray(path: Path) -> np.ndarray | None:
    # cv2.imread no abre rutas con caracteres no ASCII en Windows (p. ej. "Méndez").
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE) if data.size else None


def imwrite(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise ValueError(f"No se pudo codificar {path}")
    buf.tofile(str(path))


def preprocess(img: np.ndarray, binarize: bool) -> np.ndarray:
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if binarize:
        _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return img


def _match(img: np.ndarray, tpl: np.ndarray) -> np.ndarray:
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    # Zonas de color plano dan división por cero (inf/nan): no son coincidencias.
    return np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)


@dataclass
class Templates:
    label: np.ndarray
    digits: dict[int, np.ndarray]

    @classmethod
    def load(cls, folder: Path, binarize: bool = False) -> "Templates":
        label_path = folder / "rkm_label.png"
        if not label_path.exists():
            raise FileNotFoundError(f"Falta la plantilla {label_path}")
        label = imread_gray(label_path)
        if label is None:
            raise ValueError(f"No se pudo leer {label_path}")

        digits: dict[int, np.ndarray] = {}
        for d in range(10):
            path = folder / "digits" / f"{d}.png"
            if path.exists():
                img = imread_gray(path)
                if img is None:
                    raise ValueError(f"No se pudo leer {path}")
                digits[d] = preprocess(img, binarize)
        if not digits:
            raise FileNotFoundError(f"No hay plantillas de dígitos en {folder / 'digits'}")
        missing = sorted(set(range(10)) - digits.keys())
        if missing:
            log.warning("Faltan plantillas de los dígitos %s: esos números no se podrán leer", missing)
        return cls(label=preprocess(label, binarize), digits=digits)


@dataclass(frozen=True)
class Reading:
    value: int | None
    label_score: float


class RkmReader:
    def __init__(
        self,
        templates: Templates,
        digits_width: int = 160,
        digits_pad: int = 6,
        label_threshold: float = 0.75,
        digit_threshold: float = 0.90,
        binarize: bool = False,
        number_box: tuple[int, int, int, int] | None = None,
        digit_margin: float = 0.08,
    ):
        self.t = templates
        lh, lw = templates.label.shape
        # Zona del número relativa a la esquina superior izquierda de la etiqueta (dx, dy, ancho, alto).
        # Calibrada en el panel para saltarse el escudo del equipo; si no, franja a la derecha de "RKM".
        self.number_box = number_box or (lw, -digits_pad, digits_width, lh + 2 * digits_pad)
        self.label_threshold = label_threshold
        self.digit_threshold = digit_threshold
        self.digit_margin = digit_margin
        self.binarize = binarize
        self.max_digit_w = max(img.shape[1] for img in templates.digits.values())

    def read(self, roi: np.ndarray) -> Reading:
        roi = preprocess(roi, self.binarize)
        lh, lw = self.t.label.shape
        if roi.shape[0] < lh or roi.shape[1] < lw:
            raise ValueError("La plantilla rkm_label.png es más grande que el recorte ROI")

        res = _match(roi, self.t.label)
        _, score, _, (lx, ly) = cv2.minMaxLoc(res)
        if score < self.label_threshold:
            return Reading(None, score)  # marcador oculto o tapado en este fotograma

        dx, dy, w, h = self.number_box
        x0, y0 = max(0, lx + dx), max(0, ly + dy)
        x1, y1 = min(roi.shape[1], lx + dx + w), min(roi.shape[0], ly + dy + h)
        strip = roi[y0:y1, x0:x1]
        return Reading(self._read_digits(strip) if strip.size else None, score)

    def _read_digits(self, strip: np.ndarray) -> int | None:
        hits: list[tuple[float, int, int, int]] = []  # (score, x, ancho, dígito)
        for digit, tpl in self.t.digits.items():
            th, tw = tpl.shape
            if strip.shape[0] < th or strip.shape[1] < tw:
                continue
            res = _match(strip, tpl)
            # Mejor coincidencia por columna, para no acumular miles de candidatos. Se guardan
            # también las casi-coincidencias para poder detectar lecturas ambiguas.
            col_best = res.max(axis=0)
            for x in np.flatnonzero(col_best >= self.digit_threshold - self.digit_margin):
                hits.append((float(col_best[x]), int(x), tw, digit))
        strong = sorted((h for h in hits if h[0] >= self.digit_threshold), reverse=True)
        if not strong:
            return None

        def overlap(x, w, kx, kw):
            return min(x + w, kx + kw) - max(x, kx) > min(w, kw) // 2

        # Supresión de no-máximos en horizontal: dos dígitos no pueden solaparse más de la mitad.
        kept: list[tuple[int, int, int]] = []  # (x, ancho, dígito)
        for score, x, w, digit in strong:
            if any(overlap(x, w, kx, kw) for kx, kw, _ in kept):
                continue
            # Si otro dígito casi empata en el mismo sitio, mejor no adivinar: suele ser un
            # dígito sin plantilla que se parece a uno conocido (p. ej. un 8 leído como 3).
            if any(d != digit and s >= score - self.digit_margin and overlap(x, w, hx, hw)
                   for s, hx, hw, d in hits):
                return None
            kept.append((x, w, digit))
        kept.sort()

        # Solo el primer grupo contiguo: lo que haya más a la derecha es otra cosa del marcador.
        number = [kept[0]]
        for x, w, digit in kept[1:]:
            prev_x, prev_w, _ = number[-1]
            if x - (prev_x + prev_w) > self.max_digit_w // 2:
                break
            number.append((x, w, digit))
        return int("".join(str(d) for _, _, d in number))


@dataclass(frozen=True)
class Change:
    at_s: float  # primer fotograma en que se ve el número nuevo
    old: int
    new: int
    # Último fotograma con el número anterior. En FC el marcador se oculta durante la
    # celebración y la repetición, así que este instante es el que queda pegado al gol.
    last_old_s: float


@dataclass
class ChangeDetector:
    """Convierte lecturas sueltas (con ruido) en subidas confirmadas del contador."""

    stable_frames: int = 2
    max_jump: int = 3  # 0 = sin límite
    # Recibe el segundo de una lectura confirmada pero imposible (bajada que no es a 0, salto
    # mayor que max_jump): casi siempre un dígito sin plantilla leído como otro parecido.
    on_suspicious: object = None
    value: int | None = None
    _candidate: int | None = field(default=None, init=False)
    _candidate_at: float = field(default=0.0, init=False)
    _candidate_count: int = field(default=0, init=False)
    _value_seen_at: float = field(default=0.0, init=False)

    def feed(self, at_s: float, reading: int | None) -> Change | None:
        if reading is None:
            return None  # fotograma sin marcador legible: ni confirma ni rompe la racha
        if reading == self.value:
            self._candidate, self._candidate_count = None, 0
            self._value_seen_at = at_s
            return None
        if reading != self._candidate:
            self._candidate, self._candidate_at, self._candidate_count = reading, at_s, 1
        else:
            self._candidate_count += 1
        if self._candidate_count < self.stable_frames:
            return None

        old, new, at, last_old = self.value, self._candidate, self._candidate_at, self._value_seen_at
        self.value, self._candidate, self._candidate_count = new, None, 0
        self._value_seen_at = at_s
        if old is None:
            log.debug("Valor inicial de RKM: %s (%.0f s)", new, at)
            return None
        if new < old:
            # Bajar a 0 es un partido nuevo; cualquier otra bajada no ocurre en un partido.
            log.info("RKM bajó de %s a %s en %.0f s (partido nuevo o lectura errónea)", old, new, at)
            if new != 0 and self.on_suspicious:
                self.on_suspicious(at)
            return None
        if self.max_jump and new - old > self.max_jump:
            log.warning("Salto de RKM %s→%s en %.0f s ignorado (MAX_JUMP=%s)", old, new, at, self.max_jump)
            if self.on_suspicious:
                self.on_suspicious(at)
            return None
        return Change(at, old, new, last_old)


UNKNOWN_RUN = 3  # segundos seguidos con "RKM" visible y número ilegible para darlo por desconocido


def detect_changes(frames, reader: RkmReader, detector: ChangeDetector, on_frame=None,
                   on_unknown=None) -> list[Change]:
    """on_frame(segundo) se llama en cada fotograma; puede lanzar una excepción para cancelar.

    on_unknown(segundo) avisa de un tramo en que se ve la etiqueta RKM pero el número no
    casa con ninguna plantilla: casi siempre un dígito aún sin calibrar, y por tanto un
    posible gol perdido.
    """
    changes = []
    unreadable = 0
    total = 0
    unknown_since: float | None = None
    unknown_count = 0
    for at_s, roi in frames:
        total += 1
        if on_frame:
            on_frame(at_s)
        reading = reader.read(roi)
        if reading.value is None:
            unreadable += 1
        if reading.value is None and reading.label_score >= reader.label_threshold:
            if unknown_count == 0:
                unknown_since = at_s
            unknown_count += 1
            if unknown_count == UNKNOWN_RUN and on_unknown:
                on_unknown(unknown_since)
        else:
            unknown_count = 0
        change = detector.feed(at_s, reading.value)
        if change:
            log.info("RKM %s → %s en %s", change.old, change.new, format_ts(change.at_s))
            changes.append(change)
        if total % 1800 == 0:
            log.info("Analizados %s de vídeo (%d cambios)", format_ts(at_s), len(changes))
    if total and unreadable == total:
        log.warning("No se leyó el marcador en ningún fotograma: revisa ROI_* y las plantillas")
    else:
        log.info("%d fotogramas analizados, %d sin lectura, %d subidas", total, unreadable, len(changes))
    return changes


def format_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
