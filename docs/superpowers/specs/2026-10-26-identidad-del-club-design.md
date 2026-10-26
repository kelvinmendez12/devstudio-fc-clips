# Identidad del club en el vídeo vertical — diseño

Fecha: 2026-10-26 · Estado: aprobado por Kelvin (dirección 1 «Retransmisión», con el logo de RKM)

## Objetivo

El bloque superior del vídeo vertical de TikTok (hoy «¡GOOOL!» en Impact + píldora morada plana con
«RKM 0 → 1») pasa a ser un marcador de televisión con la **identidad del club**: su **logo**, el **color del
club** y tipografía de retransmisión. Debe servir a cualquier club, no solo a RKM: cada uno sube su logo y el
resto del diseño se deriva de él.

## Qué verá el espectador

- Placa oscura inclinada (como una cursiva) con el **logo grande a la izquierda, sin sello**, que rompe el borde
  de la placa. Si el logo ya contiene el nombre, no se repite en texto.
- Marcador: cifra anterior apagada (cristal tenue) + dos chevrones dibujados + cifra nueva sobre casilla del
  color del club. Con dos cifras (9 → 10) el cuerpo baja y las casillas se ensanchan.
- Pestaña «¡GOOOL!» pegada arriba de la placa, con tres rayas de velocidad.
- Borde del recuadro de la jugada en el color del club (corrige un error: hoy sale rosa por un color mal escrito).
- Canal y «EA FC · Clubes Pro» suben fuera de la franja que TikTok tapa.
- Etiqueta «REPETICIÓN» con el color del club.
- Entrada del bloque con fundido + deslizamiento de 0,35 s en el tramo normal; la repetición queda fija (se
  ralentiza 2× después del overlay).

Maquetas de referencia: `C:\Users\Public\fcclips-design\mockups\` (`rkm_logo_0a1.png`, `rkm_logo_movil_360.png`).

## Decisiones

1. Un solo estilo en esta versión («Retransmisión»); el dibujo se organiza para poder añadir otros después.
2. Los vídeos nuevos usan el diseño nuevo siempre. Los antiguos no se tocan; un botón por VOD «Rehacer con el
   nuevo diseño» los rehace en masa. Los ya enviados a TikTok se excluyen (su borrador cuenta en el cupo).
3. Marcador «antes → después». Título editable y cambio dramático del marcador en el instante del gol: versión
   posterior.
4. Logo: PNG, JPG o WebP. SVG y GIF se rechazan (SVG exige rasterizador y es superficie de ataque; GIF puede ir
   animado). El formato se detecta con Pillow, no por extensión.
5. Sin logo: escudo generado con las iniciales del club.
6. Color principal: sacado del logo (tono dominante saturado, ignorando blancos, negros y grises); editable a
   mano. De ese único color derivan placa, brillos, acento y color del texto (contraste ≥ 3,6:1).
7. Compilaciones: no se tocan (se recodifican desde los verticales). Mezclar goles de distinto diseño solo avisa.

## Arquitectura

| Módulo | Responsabilidad |
|---|---|
| `rkm_clips/fonts.py` | Carga de fuentes empaquetadas desde bytes (rutas con «é»), caché por (archivo, tamaño), lock, fallback a Windows/DejaVu. |
| `rkm_clips/assets/fonts/` | Barlow Condensed (ExtraBold Italic, Black Italic, ExtraBold), texto OFL y `FONTS.md`. |
| `rkm_clips/brand.py` | Ajustes (`meta.club_brand`), `ingest_logo`, `extract_colors`, `Palette`, `load_brand`, `brand_hash`. |
| `rkm_clips/scoreboard_art.py` | Dibujo con Pillow a 3× (formas), `render_header/footer/replay_tag`, escudo de iniciales, `render_preview`, `ART_VERSION`. |
| `rkm_clips/vertical.py` | `render_vertical(..., brand=)`, capas de la marca, fundido de entrada, color de las líneas desde la paleta. |
| `rkm_clips/web.py` + `templates/club.html` + `static/club.js` | Pantalla «Identidad del club», subida del logo, vista previa en vivo. |

Datos: logo normalizado en `data_dir/club/logo.png` (archivo, no BLOB); ajustes en una clave `club_brand` de `meta`.
`clips.tiktok_design` guarda el hash de la marca con que se hizo cada vertical (vacío en los antiguos).

## Logo

Al subir: orientar por EXIF, pasar a RGBA, si no tiene alfa y las 4 esquinas son del mismo color quitar el
fondo liso por inundación, recortar márgenes, redimensionar a ≤ 512 px y guardar solo el PNG normalizado.
Límites: 5 MB, 64–4096 px de lado, se capturan imágenes corruptas y bombas de descompresión. Si el logo no
tiene fondo limpio (foto), va siempre sobre un sello redondo recortado en círculo.

## Pruebas

Paleta y contraste (valores conocidos), `ingest_logo` (transparente, JPG con fondo, SVG/GIF/corrupto/enorme),
capas (tamaño, determinismo, nombre largo, dos y tres cifras, sin logo, fuentes ausentes), `build_filter` y
render real con ffmpeg, endpoints web (subida, límites, 401, vista previa), rehacer en masa y migración de la
columna.

## Privacidad y licencias

El logo vive solo en el equipo del usuario y sale únicamente dentro de los vídeos que él envíe a TikTok. Nada
del logo de RKM entra en el repositorio. Fuentes bajo SIL OFL 1.1 con su texto de licencia. Los derechos del
logo son responsabilidad de quien lo sube (se añade a las condiciones de uso).

## Fases

1. Fuentes y carga fiable. 2. Logo, color y paleta. 3. Motor de dibujo. 4. Integración en ffmpeg y render.
5. Pantalla «Identidad del club». 6. Rehacer en masa y compatibilidad. 7. Documentación y prueba real.
