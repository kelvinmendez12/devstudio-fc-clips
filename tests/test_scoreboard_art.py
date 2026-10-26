import io
import time

import numpy as np
import pytest
from PIL import Image, ImageDraw

from rkm_clips import brand as B
from rkm_clips import fonts, scoreboard_art as A
from rkm_clips import vertical


def shield_logo(w=420, h=520, color=(210, 171, 67)):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(10, 10), (w - 10, 10), (w - 10, h * 0.6), (w / 2, h - 10), (10, h * 0.6)], fill=(15, 15, 18, 255),
              outline=color + (255,), width=14)
    d.rectangle((w * 0.25, h * 0.25, w * 0.75, h * 0.45), fill=color + (255,))
    return im


def make(team="RKM", logo=True, primary="#D2AB43", show_name=False, clean=True):
    s = B.BrandSettings(primary=primary, show_name=show_name, logo_digest="x" if logo else "", logo_clean=clean)
    return B.make_brand(s, team, shield_logo() if logo else None)


def content_box(img):
    return img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()


def test_constants_match_the_video_layout():
    assert (A.W, A.H, A.BOX_Y, A.BOX) == (vertical.W, vertical.H, vertical.BOX_Y, vertical.BOX)


@pytest.mark.parametrize("logo", [True, False])
def test_header_size_mode_and_determinism(logo):
    b = make(logo=logo)
    a1, a2 = A.render_header(b, 0, 1), A.render_header(b, 0, 1)
    assert a1.size == (A.W, A.HEADER_H) and a1.mode == "RGBA"
    assert a1.tobytes() == a2.tobytes()


def test_header_changes_with_the_score_the_color_and_the_logo():
    base = A.render_header(make(), 0, 1).tobytes()
    assert A.render_header(make(), 1, 2).tobytes() != base
    assert A.render_header(make(primary="#1E88E5"), 0, 1).tobytes() != base
    assert A.render_header(make(logo=False), 0, 1).tobytes() != base


@pytest.mark.parametrize("team,old,new", [
    ("RKM", 0, 1), ("RKM", 9, 10), ("RKM", 99, 100),
    ("REAL MADRIDISTAS DEL NORTE", 2, 3), ("A" * 40, 1, 2), ("ÑANDÚ ÓPTIMO", 10, 11), ("", 0, 1),
])
@pytest.mark.parametrize("logo", [True, False])
def test_nothing_is_cut_off_for_long_names_and_big_scores(team, old, new, logo):
    for show_name in ((False,) if not logo else (False, True)):
        img = A.render_header(make(team, logo=logo, show_name=show_name), old, new)
        l, t, r, b = content_box(img)
        assert l >= 8 and r <= A.W - 8, (l, r)      # nada pegado o fuera del borde lateral
        assert t >= 8 and b <= A.HEADER_H - 8, (t, b)
        assert b <= A.BOX_Y - 4                      # y no pisa el recuadro de la jugada


def test_logo_layout_has_no_text_name_and_name_layout_has_it():
    plain = A.render_header(make("RKM", show_name=False), 0, 1)
    named = A.render_header(make("RKM", show_name=True), 0, 1)
    assert plain.tobytes() != named.tobytes()
    assert A._layout_for(make(show_name=False)) is A.LOGO_LAYOUT
    assert A._layout_for(make(show_name=True)) is A.NAME_LAYOUT
    assert A._layout_for(make(logo=False)) is A.NAME_LAYOUT


def test_logo_transparency_is_kept_around_the_logo():
    img = A.render_header(make(), 0, 1)
    assert img.getpixel((2, 2))[3] == 0 and img.getpixel((A.W - 3, A.HEADER_H - 3))[3] == 0


def test_dark_logo_on_a_dark_plate_still_gets_a_light_seat():
    dark = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(dark).ellipse((20, 20, 380, 380), fill=(8, 8, 10, 255))
    s = B.BrandSettings(primary="#9146FF", show_name=True, logo_digest="x", logo_clean=True)
    img = A.render_header(B.make_brand(s, "RKM", dark), 0, 1)
    lay = A.NAME_LAYOUT
    cy = (282 + 486) // 2 + lay.oy
    r, g, b, a = img.getpixel((lay.crest_cx + 105, cy))      # entre el logo y el aro: es el sello
    assert a == 255 and (r + g + b) / 3 > 180


def test_full_layer_has_footer_and_replay_tag_only_when_asked():
    b = make()
    head = A.render_header(b, 0, 1)
    normal = A.render_layer(b, 0, 1, "kangrykelvin", "EA FC · Clubes Pro", replay=False, header=head)
    replay = A.render_layer(b, 0, 1, "kangrykelvin", "EA FC · Clubes Pro", replay=True, header=head)
    assert normal.size == (A.W, A.H)
    assert content_box(normal.crop((0, A.BOX_Y + A.BOX, A.W, A.H))) is not None       # pie
    assert content_box(normal.crop((0, A.BOX_Y, A.W, A.BOX_Y + A.BOX))) is None       # nada sobre la jugada
    assert content_box(replay.crop((0, A.BOX_Y, A.W, A.BOX_Y + 200))) is not None     # etiqueta de repetición
    assert A.render_layer(b, 0, 1, "", "", replay=False, header=head).crop((0, A.BOX_Y + A.BOX, A.W, A.H)).getbbox() is None


def test_footer_stays_inside_the_canvas_with_long_texts():
    layer = A.render_layer(make(), 0, 1, "un_canal_con_nombre_larguisimo_de_twitch", "x" * 30, replay=False)
    l, t, r, b = content_box(layer.crop((0, A.BOX_Y + A.BOX, A.W, A.H)))
    assert l >= 0 and r <= A.W and b <= A.H - A.BOX_Y - A.BOX


def test_rendering_still_works_without_the_bundled_fonts(tmp_path, monkeypatch):
    monkeypatch.setattr(fonts, "FONT_DIR", tmp_path / "vacio")
    for role in fonts.ROLES:
        monkeypatch.setitem(fonts.ROLES, role, ["no-existe.ttf"])
    fonts.clear_cache()
    try:
        assert content_box(A.render_header(make(), 3, 4)) is not None
    finally:
        fonts.clear_cache()


def test_header_is_fast_enough():
    A.render_header(make(), 0, 1)        # calienta cachés de fuentes
    t = time.time()
    A.render_header(make(), 5, 6)
    assert time.time() - t < 2.0


def test_preview_is_a_png_of_the_requested_width():
    png = A.render_preview(make(), 0, 1, "kangrykelvin", "EA FC", width=360)
    im = Image.open(io.BytesIO(png))
    assert im.format == "PNG" and im.size == (360, 640)


def test_generated_crest_uses_the_initials_and_never_fails():
    pal = B.Palette("#E11D48")
    for name in ("RKM", "Real Madridistas", "", "X"):
        img = A.generated_crest(B.initials(name), pal, 200)
        assert img.size == (200, 200) and content_box(img) is not None
