# DevStudio FC Clips

Corta **un clip de cada gol de tu equipo** a partir de tus directos de Twitch en **EA FC (Clubes Pro)**.
Te conectas con tu cuenta de Twitch, eliges un VOD, pulsas **Generar clips** y la app encuentra cada gol
sola leyendo el marcador del juego. Funciona en tu PC con Windows: tus vídeos no salen de tu equipo.

- Panel en el navegador con tus VODs, miniaturas, progreso y galería de clips (reproducir, descargar, abrir carpeta).
- Modo **Automático**: procesa cada VOD nuevo en cuanto termina el directo.
- Cada clip empieza 15 s antes del gol y acaba 3 s después. Sin editar nada.
- **Versión vertical para TikTok** de cada gol, hecha sola (ver más abajo).
- Gratis, sin cuentas que crear ni claves que pegar.

## Instalar (5 minutos, Windows 10 u 11)

1. Descarga el ZIP: en la página del proyecto pulsa **Code → Download ZIP** y extráelo.
2. Doble clic en **`Instalar.bat`**. Instala solo lo que falte (Python y ffmpeg), crea el acceso directo
   «DevStudio FC Clips» en el escritorio y abre la app. No necesita ser administrador.
   *(Si prefieres no descargar nada: abre PowerShell y pega
   `irm https://raw.githubusercontent.com/kelvinmendez12/devstudio-fc-clips/main/instalar.ps1 | iex`)*
3. En la pantalla que se abre, pulsa **Conectar con Twitch**. Te sale un código de 8 letras: escríbelo en la
   página de Twitch que se abre y autoriza. La app solo lee tu nombre y tus VODs públicos.
4. **Marca el nombre de tu equipo** (1 minuto, solo la primera vez): en *Calibrar marcador* carga un
   fotograma de uno de tus VODs, escribe el nombre de tu equipo y recuadra esas letras tal como salen en el
   marcador. El resto (zona del marcador y números) ya viene preparado.
5. Vuelve a **VODs** y pulsa **Generar clips**. Los clips quedan en *Vídeos → FC Clips*.

Después, abre la app con el acceso directo del escritorio o en <http://localhost:8765>; también arranca sola
al iniciar sesión en Windows.

### Versión vertical para TikTok

Al terminar cada VOD, la app crea sola una versión **9:16** de cada gol (unos 10-15 segundos por clip):
fondo difuminado, la jugada a pantalla ancha con la cámara siguiendo la acción, un marcador de televisión
arriba con tu escudo («¡GOOOL!» y el resultado), **repetición del gol a cámara lenta** al final y tu canal
abajo. En cada clip tienes **Ver TikTok**, descargar y **copiar el texto del post** (con hashtags), y en cada
VOD un botón para crearlas todas. Dura unos 25 s, lo que mejor funciona en vídeos de este tipo.

**Identidad del club.** En **Identidad del club** (arriba) subes tu logo (PNG con fondo transparente es lo
ideal; también JPG o WebP) y la app saca sola el color de tu club: de ahí salen la placa, el marcador y los
bordes. Verás la vista previa al momento, con marcadores de una y de dos cifras. Si tu logo ya lleva el nombre,
no se repite; si no, activa «Mostrar el nombre del club junto al logo». Sin logo se dibuja un escudo con las
iniciales. Tu logo se guarda solo en tu ordenador y solo sale dentro de los vídeos que tú envíes a TikTok.
Si cambias el logo o el color, los vídeos ya hechos se marcan como «Diseño anterior» y un botón por VOD
(**Rehacer con el nuevo diseño**) los rehace; los que ya enviaste a TikTok no se tocan.

**Enviar a tu TikTok como borrador.** Con `TIKTOK_CLIENT_KEY` y `TIKTOK_CLIENT_SECRET` en el `.env` (de tu
app en developers.tiktok.com, ver `.env.example`), el panel muestra **Conectar TikTok**. Después, cada clip
vertical tiene **Enviar a TikTok** y cada VOD **Enviar a TikTok (N)**: el vídeo llega a la bandeja de tu
app de TikTok **como borrador**; tú lo revisas, pegas el texto del post (botón de copiar) y pulsas publicar.
La app nunca publica por ti. TikTok admite como máximo **5 borradores pendientes al día**; el panel cuenta
los que envías y avisa cuando te quedas sin cupo.

**Varios goles en un solo vídeo.** En cada gol con versión vertical hay una casilla **Juntar**. Marca los que
quieras, **de un mismo directo o de varios** (por ejemplo uno de ayer y dos de hoy), y pulsa **Juntar N en un
TikTok** en la barra que aparece abajo. La app los une por orden cronológico en un solo vídeo (tarda unos
segundos: se recodifica para que TikTok lo acepte) y lo deja arriba, en **Vídeos juntados**, con su texto de
post y su botón **Enviar a TikTok**. Así solo tienes que pulsar publicar, sin descargar ni editar nada, y cuenta
como un único borrador. Si regeneras un directo, los vídeos juntados que incluían sus goles se borran.

### Si algún número no se lee

Los números del 0 al 4 ya vienen calibrados. Cuando tu equipo llegue a otro (5, 6, 7…) por primera vez, el
panel avisa con **«números sin reconocer»**: pulsa *Revisar*, mira la imagen y elige qué número es. Después
pulsa **Regenerar** en ese VOD para recuperar ese gol. Es una vez por número.

### Actualizar

El panel avisa cuando hay una versión nueva; pulsa **Actualizar ahora** y la app se reinicia sola. Tus
clips y tu calibración no se tocan.

### Desinstalar

Ejecuta `desinstalar.ps1` (clic derecho → *Ejecutar con PowerShell*). Tus clips se conservan.

## Cómo funciona

1. `yt-dlp` descarga una copia del VOD en **480p** solo para analizarla (~1 GB por cada 1 h 45 min).
2. `ffmpeg` saca **1 fotograma por segundo** de la zona del marcador, escalado a 1920×1080.
3. OpenCV localiza el nombre de tu equipo y lee el número junto a él, cifra a cifra, comparando con una
   plantilla por dígito. Una cifra solo se acepta si coincide al 90 % o más **y** ningún otro dígito se le
   acerca a menos de 0,08: si hay duda, no se adivina.
4. Un número solo cuenta si se lee igual 2 segundos seguidos. Una subida confirmada es un gol; una bajada a 0
   es un partido nuevo.
5. El clip se ancla al **último segundo en que aún se veía el resultado anterior**: ese es el gol, tanto si el
   marcador se actualiza al momento como si se oculta durante la celebración o la repetición.
6. `ffmpeg` corta `[gol − 15 s, gol + 3 s]` **del stream original** (hasta 1080p), bajando solo esos segundos.
7. Si se ve el nombre del equipo pero el número no casa con ninguna plantilla, o la lectura es imposible,
   se guarda como **número sin reconocer** para revisarlo en el panel.

Medido con un VOD real de 1 h 45 min: 13 goles en 5 partidos, todos correctos, en menos de 4 minutos.

El login usa el *Device Code Grant Flow* de Twitch con un cliente **público**: no hay secreto en la app ni
nada que registrar.

## Límites

- Solo Windows (usa el Programador de tareas y winget para instalar).
- Pensado para el marcador de **EA FC Clubes Pro** visto en 16:9. Si emites con un marcador recortado o movido
  por OBS, ajusta la *Zona del marcador* en *Calibrar marcador*.
- VODs solo para suscriptores o eliminados por Twitch no se pueden descargar.
- Twitch borra los VODs a los 14 días (60 con Partner): genera los clips antes.

## Para desarrolladores

```powershell
git clone https://github.com/kelvinmendez12/devstudio-fc-clips
cd devstudio-fc-clips
powershell -ExecutionPolicy Bypass -File .\instalar.ps1     # instala en esta misma carpeta
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
```

Comandos útiles: `python -m rkm_clips serve --open` (panel con logs), `check` (ffmpeg y Twitch), `status`,
`process <id de VOD>`, `scan video.mp4 --clips`, `update`. Logs en `%LOCALAPPDATA%\DevStudioFCClips\logs`.

Ajustes opcionales en un archivo `.env` junto a la app (ver `.env.example`). Si bifurcas el proyecto, registra
tu propia aplicación **pública** en <https://dev.twitch.tv/console/apps> y pon su ID en `TWITCH_CLIENT_ID`.
