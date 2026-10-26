import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from rkm_clips import brand
from rkm_clips.brand import BrandError, Palette
from rkm_clips.config import load_config
from rkm_clips.db import Database


def png(img: Image.Image, fmt="PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def crest(color=(220, 30, 40), size=300, transparent=True, bg=(255, 255, 255)):
    """Escudo de prueba: forma de color con letra clara."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else bg + (255,))
    d = ImageDraw.Draw(im)
    d.polygon([(40, 30), (size - 40, 30), (size - 40, size // 2 + 40), (size // 2, size - 30), (40, size // 2 + 40)],
              fill=color + (255,))
    d.rectangle((size // 2 - 30, 90, size // 2 + 30, 150), fill=(250, 250, 250, 255))
    return im


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    monkeypatch.setenv("TEMPLATES_DIR", str(tmp_path / "plantillas"))
    cfg = load_config(env_file=tmp_path / "no.env")
    return cfg, Database(cfg.db_path)


# --- contraste y paleta --------------------------------------------------------------------------

def test_contrast_ratio_known_values():
    assert brand.contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21, rel=1e-3)
    assert brand.contrast_ratio((10, 20, 30), (10, 20, 30)) == pytest.approx(1)


@pytest.mark.parametrize("hexcolor", ["#D2AB43", "#FFEB3B", "#FFFFFF", "#000000", "#0B1E5B", "#E11D48", "#00E5FF", "#7CFC00"])
def test_palette_text_is_always_readable_on_the_accent(hexcolor):
    pal = Palette(hexcolor)
    assert brand.contrast_ratio(pal.ink, pal.accent) >= 3.0
    # y el acento se ve sobre la placa oscura (incluso el negro y el azul marino)
    assert brand.contrast_ratio(pal.accent, pal.plate) >= 3.0


def test_ffmpeg_accent_is_written_rrggbb():
    assert Palette("#D2AB43").accent_ffmpeg == "0xD2AB43"      # antes se escribía al revés: el morado salía rosa (0xFF4691)


# --- logo ----------------------------------------------------------------------------------------

def test_png_with_transparency_is_cropped_and_clean():
    res = brand.ingest_logo(png(crest()))
    assert res.clean and max(res.width, res.height) <= 512
    assert res.width < 300 and res.colors          # recortó los márgenes y encontró el color
    assert Image.open(io.BytesIO(res.png)).mode == "RGBA"


def test_white_background_jpg_loses_its_flat_background():
    res = brand.ingest_logo(png(crest(transparent=False).convert("RGB"), "JPEG", quality=92))
    assert res.clean
    im = Image.open(io.BytesIO(res.png))
    assert im.getchannel("A").getextrema()[0] == 0     # hay píxeles transparentes


def test_remove_bg_can_be_disabled():
    res = brand.ingest_logo(png(crest(transparent=False).convert("RGB"), "JPEG"), remove_bg=False)
    assert not res.clean


def test_photo_background_is_kept_and_not_clean():
    a = np.zeros((200, 200, 3), np.uint8)
    a[..., 0] = np.linspace(0, 255, 200)[None, :]
    a[..., 1] = np.linspace(255, 0, 200)[:, None]
    a[..., 2] = 120
    res = brand.ingest_logo(png(Image.fromarray(a, "RGB")))
    assert not res.clean


def test_big_logo_is_shrunk():
    res = brand.ingest_logo(png(crest(size=1500)))
    assert max(res.width, res.height) <= 512


@pytest.mark.parametrize("payload,message", [
    (b"", "ningún archivo"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "PNG, JPG o WebP"),
    (b"esto no es una imagen", "dañado"),
    (png(crest())[:200], "dañado"),
])
def test_invalid_uploads_are_rejected_in_spanish(payload, message):
    with pytest.raises(BrandError, match=message):
        brand.ingest_logo(payload)


def test_gif_is_rejected():
    with pytest.raises(BrandError, match="PNG, JPG o WebP"):
        brand.ingest_logo(png(crest().convert("P"), "GIF"))


def test_size_limits():
    with pytest.raises(BrandError, match="muy pequeño"):
        brand.ingest_logo(png(Image.new("RGBA", (40, 40), (255, 0, 0, 255))))
    with pytest.raises(BrandError, match="muy grande"):
        brand.ingest_logo(png(Image.new("L", (5000, 100), 255)))
    with pytest.raises(BrandError, match="5 MB"):
        brand.ingest_logo(b"x" * (brand.MAX_UPLOAD_BYTES + 1))


def test_empty_transparent_logo_is_rejected():
    with pytest.raises(BrandError, match="vacío"):
        brand.ingest_logo(png(Image.new("RGBA", (200, 200), (0, 0, 0, 0))))


def test_webp_is_accepted():
    assert brand.ingest_logo(png(crest(), "WEBP")).clean


# --- color del logo ------------------------------------------------------------------------------

def test_dominant_color_is_the_vivid_one():
    colors = brand.extract_colors(crest(color=(30, 90, 220)))
    h, l, s = brand._hls(brand.hex_rgb(colors[0]))
    assert 0.55 < h < 0.70 and s >= 0.5          # azul


def test_whites_blacks_and_greys_give_no_color():
    grey = Image.new("RGBA", (200, 200), (128, 128, 128, 255))
    assert brand.extract_colors(grey) == []
    assert brand.extract_colors(Image.new("RGBA", (200, 200), (250, 250, 250, 255))) == []
    assert brand.extract_colors(Image.new("RGBA", (200, 200), (5, 5, 5, 255))) == []


def test_logo_with_lots_of_white_picks_the_accent():
    im = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
    ImageDraw.Draw(im).rectangle((80, 80, 120, 120), fill=(0, 160, 60, 255))
    h, _, _ = brand._hls(brand.hex_rgb(brand.extract_colors(im)[0]))
    assert 0.25 < h < 0.45                       # verde


def test_second_suggestion_has_a_different_hue():
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 120, 200), fill=(220, 30, 40, 255))
    d.rectangle((120, 0, 200, 200), fill=(30, 90, 220, 255))
    colors = brand.extract_colors(im)
    assert len(colors) == 2
    assert abs(brand._hls(brand.hex_rgb(colors[0]))[0] - brand._hls(brand.hex_rgb(colors[1]))[0]) > 0.1


# --- ajustes y persistencia ----------------------------------------------------------------------

def test_defaults_keep_the_current_look(env):
    cfg, db = env
    s = brand.load_settings(db)
    assert s.primary == "#9146FF" and not s.logo_digest and s.style == "retransmision"
    b = brand.load_brand(cfg, db)
    assert b.logo is None and b.palette.main == (0x91, 0x46, 0xFF)


def test_saving_a_logo_sets_the_color_from_it_the_first_time(env):
    cfg, db = env
    s = brand.save_logo(cfg, db, png(crest(color=(210, 171, 67))))
    assert s.primary_auto and s.logo_digest and s.logo_clean and s.suggested
    assert brand.logo_path(cfg).is_file()
    b = brand.load_brand(cfg, db)
    assert b.logo is not None and b.palette.main == brand.hex_rgb(s.primary)


def test_a_manual_color_survives_a_new_logo(env):
    cfg, db = env
    brand.save_logo(cfg, db, png(crest()))
    brand.update_settings(db, primary="#123456")
    s = brand.save_logo(cfg, db, png(crest(color=(30, 200, 90))))
    assert s.primary == "#123456" and not s.primary_auto
    assert s.suggested                       # pero el color del logo queda como sugerencia


def test_color_validation(env):
    _, db = env
    for bad in ("rojo", "#12345", "#GGGGGG", "", None):
        with pytest.raises(BrandError):
            brand.update_settings(db, primary=bad)
    with pytest.raises(BrandError):
        brand.update_settings(db, style="inventado")
    assert brand.update_settings(db, primary="#abcdef").primary == "#ABCDEF"


def test_names_are_trimmed(env):
    _, db = env
    assert brand.update_settings(db, short_name="  Real   Madrid  ").short_name == "Real Madrid"
    long = brand.update_settings(db, short_name="Real Madridistas del Norte").short_name
    assert len(long) <= brand.SHORT_NAME_MAX and brand.load_settings(db).short_name == long


def test_corrupt_logo_file_falls_back_without_raising(env):
    cfg, db = env
    brand.save_logo(cfg, db, png(crest()))
    brand.logo_path(cfg).write_bytes(b"roto")
    assert brand.load_brand(cfg, db).logo is None


def test_delete_logo(env):
    cfg, db = env
    brand.save_logo(cfg, db, png(crest()))
    s = brand.delete_logo(cfg, db)
    assert not brand.logo_path(cfg).exists() and not s.logo_digest


def test_brand_hash_changes_with_what_the_video_shows(env):
    cfg, db = env
    base = brand.load_brand(cfg, db).hash
    brand.update_settings(db, primary="#123456")
    colored = brand.load_brand(cfg, db).hash
    brand.save_logo(cfg, db, png(crest()))
    with_logo = brand.load_brand(cfg, db).hash
    assert len({base, colored, with_logo}) == 3
    assert brand.load_brand(cfg, db).hash == with_logo      # estable si nada cambia


def test_initials():
    assert brand.initials("RKM") == "RKM"
    assert brand.initials("Real Madridistas") == "RM"
    assert brand.initials("Los Cuatro Amigos del Norte") == "LCA"
    assert brand.initials("Barcelonistas") == "BAR"
    assert brand.initials("") == "FC"
