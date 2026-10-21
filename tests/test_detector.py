import cv2
import pytest

from conftest import scoreboard
from rkm_clips.detector import Change, ChangeDetector, RkmReader, Templates, detect_changes


@pytest.fixture
def reader(templates):
    return RkmReader(templates, digits_width=200)


@pytest.mark.parametrize("value", [0, 3, 10, 47, 118, 2026])
def test_reads_number_next_to_label(reader, templates, value):
    assert reader.read(scoreboard(value, templates)).value == value


def test_reads_with_noise_and_compression(reader, templates):
    roi = scoreboard(58, templates, noise=8)
    _, buf = cv2.imencode(".jpg", roi, [cv2.IMWRITE_JPEG_QUALITY, 70])
    roi = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    assert reader.read(roi).value == 58


def test_hidden_scoreboard_gives_no_reading(reader, templates):
    reading = reader.read(scoreboard(None, templates))
    assert reading.value is None
    assert reading.label_score < 0.75


def test_ignores_other_number_further_right(reader, templates):
    assert reader.read(scoreboard(12, templates, extra_right="9")).value == 12


def test_binarized_mode(templates):
    reader = RkmReader(Templates(templates.label, templates.digits), digits_width=200, binarize=True)
    # Las plantillas se binarizan al cargarlas; aquí se simula pasándolas ya procesadas.
    from rkm_clips.detector import preprocess
    reader.t = Templates(preprocess(templates.label, True), {d: preprocess(t, True) for d, t in templates.digits.items()})
    assert reader.read(scoreboard(31, templates, noise=5)).value == 31


def feed_all(detector, readings):
    return [c for t, v in enumerate(readings) if (c := detector.feed(float(t), v))]


def test_detects_increase_at_first_frame_of_new_value():
    changes = feed_all(ChangeDetector(stable_frames=2), [3, 3, 3, 4, 4, 4, 5, 5])
    assert changes == [Change(3.0, 3, 4, 2.0), Change(6.0, 4, 5, 5.0)]


def test_first_value_is_only_baseline():
    assert feed_all(ChangeDetector(), [7, 7, 7]) == []


def test_single_frame_glitch_is_ignored():
    assert feed_all(ChangeDetector(stable_frames=2), [3, 3, 8, 3, 3]) == []


def test_unreadable_frames_do_not_break_streak():
    changes = feed_all(ChangeDetector(stable_frames=2), [3, 3, None, 4, None, 4, 4])
    assert changes == [Change(3.0, 3, 4, 1.0)]


def test_decrease_resets_baseline_without_clip():
    assert feed_all(ChangeDetector(), [5, 5, 0, 0, 1, 1]) == [Change(4.0, 0, 1, 3.0)]


def test_scoreboard_hidden_after_goal_keeps_last_old_time():
    # En FC el marcador desaparece durante la celebración y la repetición.
    changes = feed_all(ChangeDetector(), [1, 1, 1] + [None] * 20 + [2, 2])
    assert changes == [Change(23.0, 1, 2, 2.0)]


def test_number_box_skips_team_crest(templates):
    import cv2 as _cv2
    roi = scoreboard(4, templates)
    # Escudo redondo entre "RKM" y el número, como en el marcador de FC.
    lh, lw = templates.label.shape
    _cv2.circle(roi, (20 + lw + 9, 110 + lh // 2), 7, 235, 2)
    reader = RkmReader(templates, number_box=(lw + 18, -4, 80, lh + 8))
    assert reader.read(roi).value == 4


def test_large_jump_is_ignored_as_misread():
    det = ChangeDetector(max_jump=3)
    assert feed_all(det, [2, 2, 9, 9, 2, 2, 3, 3]) == [Change(6.0, 2, 3, 5.0)]


def test_detect_changes_over_frames(reader, templates):
    values = [1] * 5 + [None] * 2 + [2] * 6 + [3] * 4
    frames = ((float(t), scoreboard(v, templates)) for t, v in enumerate(values))
    changes = detect_changes(frames, reader, ChangeDetector(stable_frames=2))
    assert changes == [Change(7.0, 1, 2, 4.0), Change(13.0, 2, 3, 12.0)]


def test_unknown_number_with_label_visible_is_reported_once_per_run(templates):
    # Sin plantilla del 5: se ve "RKM" pero el número no se puede leer.
    partial = Templates(templates.label, {d: t for d, t in templates.digits.items() if d != 5})
    reader = RkmReader(partial, digits_width=200)
    values = [4] * 3 + [5] * 6 + [None] * 2 + [5] * 4
    frames = ((float(t), scoreboard(v, templates)) for t, v in enumerate(values))
    unknown = []
    detect_changes(frames, reader, ChangeDetector(), on_unknown=unknown.append)
    # Un aviso por tramo (al tercer segundo seguido), con el instante en que empezó.
    assert unknown == [3.0, 11.0]
