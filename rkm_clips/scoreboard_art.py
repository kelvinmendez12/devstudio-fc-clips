"""Dibujo del marcador del vídeo vertical («Retransmisión»): logo del club, cifras, pestaña de gol y pie.

Todo se dibuja con Pillow sobre capas RGBA de 1080×1920 que ffmpeg superpone. Las formas se rasterizan a 3× y
se reducen (Pillow no suaviza polígonos), el texto se dibuja como máscara y se compone en la capa.

Dos composiciones, según haya logo con letras o no:
* logo → el logo grande rompe la placa por la izquierda y no se repite el nombre en texto;
* sin logo (o «mostrar nombre») → escudo en un sello redondo y el nombre del club en la placa.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .brand import Brand, Palette, contrast_ratio, initials, logo_is_dark, mix, square
from .fonts import LOCK, get_font

W, H = 1080, 1920
HEADER_H = 640          # el bloque de arriba vive en y=0..640 (el recuadro de la jugada empieza en BOX_Y)
BOX_Y, BOX = 560, 1080
SS = 3
K = 0.20                # inclinación de la placa (tan 11,3°): la de la cursiva de Barlow
ART_VERSION = 1         # sube si cambia el aspecto: los vídeos hechos con otra versión pasan a «desactualizados»


@dataclass(frozen=True)
class Layout:
    xl: int                 # borde izquierdo de la placa (abajo)
    xr: int
    crest_cx: int
    crest_d: int
    num_size: tuple[int, int]       # cuerpo de las cifras con 1 cifra / con 2 o más
    cell_old: int                   # ancho mínimo de la casilla de la cifra anterior
    cell_new: int
    gap: int
    seat: str                       # sello del escudo: "none" | "dark" | "light" | "auto"
    show_name: bool
    oy: int = -24


LOGO_LAYOUT = Layout(xl=300, xr=990, crest_cx=200, crest_d=350, num_size=(212, 166), cell_old=200, cell_new=290,
                     gap=64, seat="none", show_name=False)
NAME_LAYOUT = Layout(xl=122, xr=962, crest_cx=178, crest_d=250, num_size=(156, 128), cell_old=110, cell_new=174,
                     gap=56, seat="auto", show_name=True)


# --- formas ---------------------------------------------------------------------------------------

def _mask(size, fn, ss: int = SS) -> Image.Image:
    m = Image.new("L", (size[0] * ss, size[1] * ss), 0)
    fn(ImageDraw.Draw(m), ss)
    return m.resize(size, Image.LANCZOS)


def poly(size, pts) -> Image.Image:
    return _mask(size, lambda d, s: d.polygon([(x * s, y * s) for x, y in pts], fill=255))


def ellipse(size, cx, cy, r) -> Image.Image:
    return _mask(size, lambda d, s: d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=255))


def para(xl, xr, y0, y1, k):
    """Paralelogramo inclinado: xl/xr son las x del borde INFERIOR; arriba se desplaza k·alto."""
    d = k * (y1 - y0)
    return [(xl + d, y0), (xr + d, y0), (xr, y1), (xl, y1)]


def chevron(size, cx, cy, h, t, w):
    x = cx - (t + w) / 2
    return poly(size, [(x, cy - h / 2), (x + t, cy - h / 2), (x + t + w, cy), (x + t, cy + h / 2), (x, cy + h / 2), (x + w, cy)])


def bezier(p0, p1, p2, p3, n=24):
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def vgrad(size, y0, y1, c0, c1) -> Image.Image:
    ys = np.clip((np.arange(size[1]) - y0) / max(1e-6, (y1 - y0)), 0, 1)[:, None]
    arr = np.zeros((size[1], size[0], 4), np.float32)
    for i in range(3):
        arr[..., i] = c0[i] + (c1[i] - c0[i]) * ys
    arr[..., 3] = 255
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def fill(layer: Image.Image, mask: Image.Image, src, alpha: float = 1.0) -> None:
    """Rellena `mask` sobre la capa con un color o con una imagen RGBA del tamaño de la capa."""
    if isinstance(src, tuple):
        img = Image.new("RGBA", layer.size, tuple(src[:3]) + (255,))
    else:
        img = src.copy()
    m = ImageChops.multiply(mask, img.getchannel("A"))
    if alpha < 1:
        m = m.point(lambda v: int(v * alpha))
    img.putalpha(m)
    layer.alpha_composite(img)


def shadow(layer, mask, dy=10, blur=16, opacity=0.55):
    fill(layer, ImageChops.offset(mask, 0, dy).filter(ImageFilter.GaussianBlur(blur)), (0, 0, 0), opacity)


def glow(layer, mask, color, blur=14, opacity=0.8):
    fill(layer, mask.filter(ImageFilter.GaussianBlur(blur)), color, opacity)


def edge_light(layer, mask, top_a=0.30, bottom_a=0.35, dy=2):
    """Bisel fino: línea de luz arriba y de sombra abajo, DENTRO de la forma."""
    fill(layer, ImageChops.subtract(mask, ImageChops.offset(mask, 0, dy)), (255, 255, 255), top_a)
    fill(layer, ImageChops.subtract(mask, ImageChops.offset(mask, 0, -dy)), (0, 0, 0), bottom_a)


def hatch(size, spacing=6, thick=1, slope=1.0) -> Image.Image:
    yy, xx = np.mgrid[0:size[1], 0:size[0]]
    return Image.fromarray((((xx + slope * yy) % spacing) < thick).astype(np.uint8) * 255, "L")


# --- texto ----------------------------------------------------------------------------------------

def text_w(txt: str, f, tracking: float = 0) -> float:
    if not tracking:
        return f.getlength(txt)
    return sum(f.getlength(c) for c in txt) + tracking * (len(txt) - 1)


def cap_h(f, ref: str = "H") -> int:
    return -f.getbbox(ref, anchor="ls")[1]


def draw_text(layer, x, y_base, txt, f, color=(255, 255, 255), tracking=0.0, anchor="l", alpha=1.0,
              shadow_=None, stroke=0, stroke_color=(0, 0, 0)):
    """Texto como máscara con línea base en y_base. shadow_ = (dx, dy, blur, color, opacidad)."""
    size = layer.size
    tw = text_w(txt, f, tracking)
    x0 = x - (tw / 2 if anchor == "c" else tw if anchor == "r" else 0)

    def mask(stroke_w=0):
        m = Image.new("L", size, 0)
        d = ImageDraw.Draw(m)
        if not tracking:
            d.text((x0, y_base), txt, font=f, fill=255, anchor="ls", stroke_width=stroke_w)
        else:
            cx = x0
            for ch in txt:
                d.text((cx, y_base), ch, font=f, fill=255, anchor="ls", stroke_width=stroke_w)
                cx += f.getlength(ch) + tracking
        return m

    m = mask()
    if shadow_:
        dx, dy, blur, col, op = shadow_
        sm = ImageChops.offset(m, dx, dy)
        fill(layer, sm.filter(ImageFilter.GaussianBlur(blur)) if blur else sm, col, op)
    if stroke:
        fill(layer, mask(stroke), stroke_color, alpha)
    fill(layer, m, tuple(color), alpha)


def fit_lines(name: str, role: str, max_w: int, max_size: int, min_size: int, tracking: float = 0,
              two_line_scale: float = 0.78):
    """Nombre del club → (líneas, fuente): una línea y, si no cabe a ≥ min_size, dos partiendo por el espacio
    más centrado; último recurso: tamaño mínimo y elipsis."""
    name = " ".join(name.upper().split())
    for size in range(max_size, min_size - 1, -2):
        f = get_font(role, size)
        if text_w(name, f, tracking) <= max_w:
            return [name], f
    if " " in name:
        words = name.split()
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if best is None or max(len(a), len(b)) < best[0]:
                best = (max(len(a), len(b)), a, b)
        _, a, b = best
        for size in range(int(max_size * two_line_scale), int(min_size * 0.6), -2):
            f = get_font(role, size)
            if max(text_w(a, f, tracking), text_w(b, f, tracking)) <= max_w:
                return [a, b], f
    f = get_font(role, min_size)
    while text_w(name, f, tracking) > max_w and len(name) > 3:
        name = name[:-2].rstrip() + "…"
    return [name], f


# --- escudo ---------------------------------------------------------------------------------------

def _shield_pts(cx, top, w, h, ss):
    x0, x1 = cx - w / 2, cx + w / 2
    mid, bot = top + h * 0.52, top + h
    right = bezier((x1, mid), (x1, top + h * 0.80), (cx + w * 0.22, top + h * 0.93), (cx, bot))
    left = bezier((cx, bot), (cx - w * 0.22, top + h * 0.93), (x0, top + h * 0.80), (x0, mid))
    return [(x * ss, y * ss) for x, y in [(x0, top), (x1, top)] + right + left]


def generated_crest(text: str, pal: Palette, size: int = 400) -> Image.Image:
    """Escudo con las iniciales del club, para quien aún no ha subido su logo."""
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, top, w, h = S / 2, S * 0.03, S * 0.88, S * 0.94

    def pts(inset):
        i = S * inset
        return _shield_pts(cx / ss, (top + i) / ss, (w - 2 * i) / ss, (h - 2 * i) / ss, ss)

    d.polygon(pts(0), fill=pal.plate_lo + (255,))
    d.polygon(pts(0.022), fill=pal.light + (255,))
    inner = Image.new("L", img.size, 0)
    ImageDraw.Draw(inner).polygon(pts(0.055), fill=255)
    base = Image.new("RGBA", img.size, pal.plate + (255,))
    b = ImageDraw.Draw(base)
    b.polygon([(0, S * 0.30), (S, S * 0.02), (S, S * 0.50), (0, S * 0.78)], fill=pal.accent + (255,))
    b.polygon([(0, S * 0.80), (S, S * 0.52), (S, S * 0.58), (0, S * 0.86)], fill=pal.light + (255,))
    img.paste(base, (0, 0), inner)
    d = ImageDraw.Draw(img)
    f = get_font("text", int(S * (0.36 if len(text) <= 2 else 0.29)))
    d.text((S / 2, S * 0.56), text, font=f, fill=(255, 255, 255, 255), anchor="mm",
           stroke_width=int(S * 0.012), stroke_fill=pal.plate_lo + (255,))
    return img.resize((size, size), Image.LANCZOS)


def place_crest(layer, crest: Image.Image, cx, cy, d, pal: Palette, seat: str, clean: bool, ring_px: int = 9):
    """Coloca el escudo o logo. Con sello, un disco claro u oscuro garantiza que se vea sea del color que sea;
    un logo sin fondo limpio (foto) va siempre sobre sello y recortado en círculo."""
    size = layer.size
    if seat == "auto":
        seat = "light" if logo_is_dark(crest) else "dark"
    if not clean and seat == "none":
        seat = "dark"
    if seat != "none":
        disc = ellipse(size, cx, cy, d / 2)
        shadow(layer, disc, dy=10, blur=14, opacity=0.6)
        fill(layer, disc, vgrad(size, cy - d / 2, cy + d / 2, pal.light, pal.dark))
        inner = ellipse(size, cx, cy, d / 2 - ring_px)
        hi, lo = (pal.plate_hi, pal.plate_lo) if seat == "dark" else ((244, 244, 246), (214, 216, 222))
        fill(layer, inner, vgrad(size, cy - d / 2, cy + d / 2, hi, lo))
        edge_light(layer, inner, top_a=0.22, bottom_a=0.0)
        inner_d = d - ring_px * 2
        pad = 0.09
    else:
        inner_d, pad = d, 0.0
    tgt = int(inner_d * (1 - 2 * pad))
    c = crest.resize((tgt, tgt), Image.LANCZOS)
    if not clean:
        m = Image.new("L", c.size, 0)
        ImageDraw.Draw(m).ellipse((0, 0, c.width - 1, c.height - 1), fill=255)
        c = c.copy()
        c.putalpha(ImageChops.multiply(c.getchannel("A"), m))
    sh = Image.new("RGBA", size, (0, 0, 0, 0))
    sh.paste(c, (int(cx - tgt / 2), int(cy - tgt / 2)), c)
    if seat == "none":
        shadow(layer, sh.getchannel("A"), dy=8, blur=10, opacity=0.6)
    layer.alpha_composite(sh)


# --- bloque superior ------------------------------------------------------------------------------

def _layout_for(brand: Brand) -> Layout:
    return NAME_LAYOUT if (brand.logo is None or brand.settings.show_name) else LOGO_LAYOUT


def render_header(brand: Brand, old: int, new: int, headline: str = "¡GOOOL!") -> Image.Image:
    """Placa con logo, marcador y pestaña de gol: capa RGBA de 1080×HEADER_H."""
    with LOCK:
        return _header(brand, old, new, headline)


def _header(brand: Brand, old: int, new: int, headline: str) -> Image.Image:
    pal, lay = brand.palette, _layout_for(brand)
    sz = (W, HEADER_H)
    L = Image.new("RGBA", sz, (0, 0, 0, 0))
    py0, py1 = 282 + lay.oy, 486 + lay.oy
    pmid = (py0 + py1) / 2
    xl, xr = lay.xl, lay.xr

    # placa
    pm = poly(sz, para(xl, xr, py0, py1, K))
    shadow(L, pm, dy=14, blur=20, opacity=0.62)
    fill(L, pm, vgrad(sz, py0, py1, pal.plate_hi, pal.plate_lo))
    fill(L, ImageChops.multiply(pm, hatch(sz)), (255, 255, 255), 0.05)
    edge_light(L, pm, top_a=0.30, bottom_a=0.4)
    fill(L, ImageChops.subtract(pm, pm.filter(ImageFilter.MinFilter(5))), (255, 255, 255), 0.10)

    # marcador
    digits = max(len(str(old)), len(str(new)))
    fnum = get_font("number", lay.num_size[0] if digits == 1 else lay.num_size[1])
    dh = cap_h(fnum, "0")
    cw_old = max(lay.cell_old, text_w(str(old), fnum) + 60)
    cw_new = max(lay.cell_new, text_w(str(new), fnum) + 70)
    ch0, ch1 = py0 + 17, py1 - 17
    cmid = (ch0 + ch1) / 2
    shift = K * (ch1 - ch0) / 2
    new_r = xr + K * (py1 - ch1) - 20
    new_l = new_r - cw_new
    old_r = new_l - lay.gap
    old_l = old_r - cw_old

    mo = poly(sz, para(old_l, old_r, ch0, ch1, K))
    fill(L, mo, (255, 255, 255), 0.075)
    edge_light(L, mo, top_a=0.16, bottom_a=0.0)
    draw_text(L, old_l + cw_old / 2 + shift - K * dh / 2, cmid + dh / 2, str(old), fnum, (226, 230, 240),
              anchor="c", alpha=0.80)

    mn = poly(sz, para(new_l, new_r, ch0, ch1, K))
    glow(L, mn, pal.glow, blur=20, opacity=0.55)
    light_ink = pal.ink == (255, 255, 255)
    g0, g1 = (pal.accent, mix(pal.dark, pal.accent, 0.25)) if light_ink else (pal.light, pal.accent)
    fill(L, mn, vgrad(sz, ch0, ch1, g0, g1))
    fill(L, ImageChops.multiply(mn, hatch(sz)), (255, 255, 255), 0.06)
    edge_light(L, mn, top_a=0.45, bottom_a=0.25)
    draw_text(L, new_l + cw_new / 2 + shift - K * dh / 2, cmid + dh / 2, str(new), fnum, pal.ink, anchor="c",
              shadow_=(0, 4, 0, (0, 0, 0), 0.18) if light_ink else None)

    # flecha: dos chevrones dibujados (la fuente no tiene «→»)
    gx = (old_r + new_l) / 2 + shift
    fill(L, chevron(sz, gx - 9, cmid, 46, 11, 16), pal.accent, 0.45)
    fill(L, chevron(sz, gx + 9, cmid, 46, 11, 16), pal.accent)

    nl = lay.crest_cx + lay.crest_d / 2 + 10
    if lay.show_name:
        nr = old_l - 28
        lines, fname = fit_lines(brand.name or "GOL", "number", int(nr - nl - 14), 124, 56, tracking=1)
        ch = cap_h(fname)
        lead = ch * 1.22
        base0 = pmid - (ch + (len(lines) - 1) * lead) / 2 + ch
        for i, line in enumerate(lines):
            draw_text(L, (nl + nr) / 2, base0 + i * lead, line, fname, (255, 255, 255), tracking=1, anchor="c",
                      shadow_=(0, 4, 0, (0, 0, 0), 0.35))
        fill(L, poly(sz, para(old_l - 22, old_l - 18, py0 + 26, py1 - 26, K)), pal.accent, 0.55)

    # pestaña del titular y rayas de velocidad
    fh = get_font("headline", 86)
    hh = cap_h(fh)
    tab_l, tab_w = nl + 6, text_w(headline, fh, 3) + 70
    ty1, ty0 = py0 - 5, py0 - 5 - 96
    tm = poly(sz, para(tab_l, tab_l + tab_w, ty0, ty1, K))
    shadow(L, tm, dy=8, blur=12, opacity=0.5)
    fill(L, tm, vgrad(sz, ty0, ty1, g0, g1))
    edge_light(L, tm, top_a=0.45, bottom_a=0.25)
    draw_text(L, tab_l + tab_w / 2 + K * (ty1 - ty0) / 2 - K * hh / 2, (ty0 + ty1) / 2 + hh / 2, headline, fh,
              pal.ink, tracking=3, anchor="c")
    sx = tab_l + tab_w + 20
    for wd, a in ((26, 0.9), (16, 0.6), (8, 0.35)):
        fill(L, poly(sz, para(sx, sx + wd, ty0 + 30, ty1, K)), pal.accent, a)
        sx += wd + 12

    # escudo o logo
    if brand.logo is not None:
        crest, clean = square(brand.logo), brand.settings.logo_clean
    else:
        crest, clean = generated_crest(initials(brand.name), pal), True
    place_crest(L, crest, lay.crest_cx, pmid, lay.crest_d, pal, seat=lay.seat, clean=clean)
    return L


def _replay_tag(pal: Palette) -> Image.Image:
    """Etiqueta «REPETICIÓN» con el color del club."""
    f = get_font("text", 58)
    label = "REPETICIÓN"
    tw = text_w(label, f, 2)
    w, h = int(tw + 90), 96
    sz = (w + 40, h + 30)
    L = Image.new("RGBA", sz, (0, 0, 0, 0))
    m = poly(sz, para(14, 14 + w - K * h, 10, 10 + h, K))
    shadow(L, m, dy=6, blur=8, opacity=0.5)
    fill(L, m, vgrad(sz, 10, 10 + h, pal.plate_hi, pal.plate_lo))
    edge_light(L, m, top_a=0.25, bottom_a=0.3)
    fill(L, poly(sz, para(14, 24, 10, 10 + h, K)), pal.accent)
    draw_text(L, 14 + K * h / 2 + 24 + (w - 24 - K * h) / 2, 10 + h / 2 + cap_h(f) / 2, label, f,
              (255, 255, 255), tracking=2, anchor="c")
    return L


def render_layer(brand: Brand, old: int, new: int, channel: str, tagline: str, replay: bool,
                 header: Image.Image | None = None) -> Image.Image:
    """Capa RGBA 1080×1920 con todo lo fijo. `header` permite reutilizar el bloque ya dibujado."""
    header = header if header is not None else render_header(brand, old, new)
    with LOCK:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img.alpha_composite(header, (0, 0))
        f_ch, f_tag = get_font("text", 58), get_font("text", 46)
        d = ImageDraw.Draw(img)
        y = BOX_Y + BOX + 40
        for txt, f, fillc in ((f"twitch.tv/{channel}" if channel else "", f_ch, (255, 255, 255, 240)),
                              (tagline, f_tag, (214, 216, 226, 220))):
            if txt:
                d.text((W / 2, y), txt, font=f, fill=fillc, anchor="mt", stroke_width=4, stroke_fill=(0, 0, 0, 255))
                y += int(f.size * 1.08)
        if replay:
            tag = _replay_tag(brand.palette)
            img.alpha_composite(tag, (30, BOX_Y + 24))
        return img


# --- vista previa ---------------------------------------------------------------------------------

def _preview_background(pal: Palette) -> Image.Image:
    """Fondo que imita el del vídeo: césped difuminado y oscuro arriba, recuadro de la jugada y bordes."""
    rng = np.random.default_rng(7)
    field = np.zeros((H, W, 3), np.float32)
    field[...] = (34, 78, 36)
    stripes = (np.arange(W)[None, :] // 120) % 2
    field += stripes[..., None] * np.array([10, 16, 8], np.float32)
    field += rng.normal(0, 3, (H, W, 1)).astype(np.float32)
    img = Image.fromarray(np.clip(field, 0, 255).astype(np.uint8), "RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(30))
    dark = Image.eval(blurred, lambda v: int(v * 0.5))
    img.paste(dark.crop((0, 0, W, BOX_Y)), (0, 0))
    img.paste(dark.crop((0, BOX_Y + BOX, W, H)), (0, BOX_Y + BOX))
    d = ImageDraw.Draw(img)
    d.rectangle((0, BOX_Y - 6, W, BOX_Y - 1), fill=pal.accent)
    d.rectangle((0, BOX_Y + BOX, W, BOX_Y + BOX + 4), fill=pal.accent)
    return img.convert("RGBA")


def render_preview(brand: Brand, old: int = 0, new: int = 1, channel: str = "", tagline: str = "",
                   width: int = 540, replay: bool = False) -> bytes:
    """PNG del aspecto final sobre un fondo de ejemplo (para la pantalla «Identidad del club»)."""
    layer = render_layer(brand, old, new, channel, tagline, replay)
    out = Image.alpha_composite(_preview_background(brand.palette), layer).convert("RGB")
    out = out.resize((width, int(H * width / W)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()
