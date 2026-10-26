import pytest
from PIL import Image, ImageDraw

from rkm_clips import fonts

# lo que el diseño necesita dibujar: acentos del español, ¡ ¿, cifras y el punto medio
REQUIRED = "¡¿!ÓÍÁÉÚÑÜ·0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def test_bundled_fonts_exist_with_their_licence():
    for name in ("BarlowCondensed-ExtraBoldItalic.ttf", "BarlowCondensed-BlackItalic.ttf",
                 "BarlowCondensed-ExtraBold.ttf", "OFL-BarlowCondensed.txt", "FONTS.md"):
        assert (fonts.FONT_DIR / name).is_file(), name


@pytest.mark.parametrize("role", ["number", "headline", "text"])
def test_every_role_loads_the_bundled_font_and_has_the_needed_glyphs(role):
    font = fonts.get_font(role, 120)
    assert hasattr(font, "getmask")
    notdef = font.getmask("￿")   # un carácter que la fuente no tiene: su aspecto es el del «.notdef»
    for ch in REQUIRED:
        mask = font.getmask(ch)
        assert mask.getbbox() is not None, f"{role}: sin glifo para {ch!r}"
        assert bytes(mask) != bytes(notdef) or mask.size != notdef.size, f"{role}: {ch!r} sale como .notdef"


def test_fonts_load_from_bytes_even_with_accented_paths(tmp_path, monkeypatch):
    folder = tmp_path / "Méndez Vídeos"
    folder.mkdir()
    (folder / "BarlowCondensed-ExtraBold.ttf").write_bytes((fonts.FONT_DIR / "BarlowCondensed-ExtraBold.ttf").read_bytes())
    monkeypatch.setattr(fonts, "FONT_DIR", folder)
    fonts.clear_cache()
    try:
        img = Image.new("L", (300, 100), 0)
        ImageDraw.Draw(img).text((5, 5), "GOL 10", font=fonts.get_font("text", 80), fill=255)
        assert img.getbbox() is not None
    finally:
        fonts.clear_cache()


def test_missing_bundled_fonts_fall_back_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(fonts, "FONT_DIR", tmp_path / "vacio")
    monkeypatch.setitem(fonts.ROLES, "number", ["no-existe.ttf"])
    fonts.clear_cache()
    try:
        font = fonts.get_font("number", 60)
        img = Image.new("L", (200, 100), 0)
        ImageDraw.Draw(img).text((5, 5), "10", font=font, fill=255)
        assert img.getbbox() is not None
    finally:
        fonts.clear_cache()


def test_font_objects_are_cached():
    fonts.clear_cache()
    assert fonts.get_font("text", 50) is fonts.get_font("text", 50)
    assert fonts.get_font("text", 50) is not fonts.get_font("text", 51)
