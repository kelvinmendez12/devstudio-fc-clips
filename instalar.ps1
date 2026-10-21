# Instala DevStudio FC Clips en Windows: Python y ffmpeg si faltan, la app, una tarea que la arranca
# al iniciar sesión y un acceso directo en el escritorio. No necesita permisos de administrador.
#
# Desde el ZIP descargado:  doble clic en Instalar.bat
# Sin descargar nada:       irm https://raw.githubusercontent.com/kelvinmendez12/devstudio-fc-clips/main/instalar.ps1 | iex
#
# Opciones (para pruebas): -InstallDir, -InPlace, -NoTask, -NoShortcut, -NoStart

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "DevStudioFCClips\app"),
    [string]$Repo = "kelvinmendez12/devstudio-fc-clips",
    [string]$Branch = "main",
    [switch]$InPlace,
    [switch]$NoTask,
    [switch]$NoShortcut,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$AppName = "DevStudio FC Clips"
$TaskName = "DevStudio FC Clips"
$Port = 8765

function Step($text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Warn($text) { Write-Host $text -ForegroundColor Yellow }
function Stop-App {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "rkm_clips" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Test-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }

Write-Host "== $AppName ==" -ForegroundColor Magenta

# 1. La app -------------------------------------------------------------------------------------
Step "1/5 Preparando la app..."
$here = $PSScriptRoot
$hasLocalApp = $here -and (Test-Path (Join-Path $here "rkm_clips\__init__.py"))
if ($hasLocalApp -and ($InPlace -or (Test-Path (Join-Path $here ".git")))) {
    $root = $here   # clon de git o carpeta de desarrollo: se usa donde está
} else {
    $root = $InstallDir
    New-Item -ItemType Directory -Force $root | Out-Null
    Stop-App
    if ($hasLocalApp) {
        $source = $here
    } else {
        $tmp = Join-Path $env:TEMP ("fcclips_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory $tmp | Out-Null
        $zip = Join-Path $tmp "app.zip"
        Write-Host "Descargando la última versión..."
        Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
        Expand-Archive $zip -DestinationPath $tmp
        $source = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
    }
    # Copia sin pisar el entorno de Python ni la configuración propia.
    robocopy $source $root /E /XD .venv .git __pycache__ /XF .env /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "No se pudo copiar la app a $root" }
    try {
        $ref = Invoke-RestMethod -Headers @{ Accept = "application/vnd.github.sha" } "https://api.github.com/repos/$Repo/commits/$Branch"
        Set-Content -Path (Join-Path $root ".installed_ref") -Value ([string]$ref).Trim() -Encoding ascii
    } catch { }   # sin esto solo se pierde el aviso de «hay una versión nueva»
}
Write-Host "Carpeta de la app: $root"

# 2. Python -------------------------------------------------------------------------------------
Step "2/5 Python..."
function Find-Python {
    $candidates = @(@("py", "-3.12"), @("py", "-3.13"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3"), @("python"))
    foreach ($c in $candidates) {
        try {
            $rest = @()
            if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
            $v = & $c[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.10") { return ,$c }
        } catch { }
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    if (-not (Test-Winget)) { throw "Falta Python 3.10 o superior. Instálalo desde https://www.python.org/downloads/ y vuelve a ejecutar esto." }
    Write-Host "Instalando Python 3.12 (puede tardar un par de minutos)..."
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $direct = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $direct) { $py = @($direct) } else { $py = Find-Python }
    if (-not $py) { throw "No se pudo instalar Python automáticamente. Instálalo desde python.org y vuelve a ejecutar esto." }
}
Write-Host ("Python: " + ($py -join " "))

# 3. ffmpeg -------------------------------------------------------------------------------------
Step "3/5 ffmpeg..."
$envFile = Join-Path $root ".env"
function Set-EnvValue($name, $value) {
    $lines = @()
    if (Test-Path $envFile) { $lines = @(Get-Content $envFile -Encoding UTF8 | Where-Object { $_ -notmatch "^$name=" }) }
    $lines += "$name=$value"
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (-not (Test-Winget)) { throw "Falta ffmpeg. Instálalo desde https://www.gyan.dev/ffmpeg/builds/ y vuelve a ejecutar esto." }
    Write-Host "Instalando ffmpeg..."
    winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $found = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $found) { throw "No se pudo instalar ffmpeg automáticamente. Instálalo a mano y vuelve a ejecutar esto." }
    # El PATH de esta sesión no se actualiza hasta volver a iniciar sesión: se guarda la ruta completa.
    Set-EnvValue "FFMPEG_PATH" $found.FullName
    Write-Host "ffmpeg: $($found.FullName)"
} else {
    Write-Host "ffmpeg: $((Get-Command ffmpeg).Source)"
}

# 4. Entorno de Python y dependencias -----------------------------------------------------------
Step "4/5 Instalando dependencias (la primera vez tarda unos minutos)..."
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $rest = @()
    if ($py.Count -gt 1) { $rest = $py[1..($py.Count - 1)] }
    & $py[0] @rest -m venv $venv
    if (-not (Test-Path $pythonw)) { throw "No se pudo crear el entorno de Python." }
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias." }

# 5. Arranque automático y acceso directo -------------------------------------------------------
Step "5/5 Acceso directo y arranque automático..."
# Versiones anteriores se llamaban «RKM Clips»: se retiran para no duplicar.
Unregister-ScheduledTask -TaskName "RKM Clips" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "RKM Clips.lnk") -ErrorAction SilentlyContinue

if (-not $NoTask) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m rkm_clips serve" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "$AppName (http://localhost:$Port): revisa tus VODs de Twitch y corta los goles." -Force | Out-Null
    Write-Host "La app se abrirá sola al iniciar sesión en Windows."
}
if (-not $NoShortcut) {
    $shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m rkm_clips serve --open"
    $lnk.WorkingDirectory = $root
    $lnk.Description = "Abrir $AppName"
    $lnk.Save()
    Write-Host "Acceso directo «$AppName» creado en el escritorio."
}

if (-not $NoStart) {
    Stop-App
    Start-Sleep -Seconds 1
    if (-not $NoTask) { Start-ScheduledTask -TaskName $TaskName }
    else { Start-Process $pythonw -ArgumentList "-m rkm_clips serve" -WorkingDirectory $root }
    $up = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 1
        try { Invoke-WebRequest -UseBasicParsing "http://localhost:$Port/" -TimeoutSec 2 -MaximumRedirection 0 -ErrorAction Stop | Out-Null; $up = $true; break }
        catch { if ($_.Exception.Response) { $up = $true; break } }
    }
    if ($up) {
        Start-Process "http://localhost:$Port"
        Write-Host "`nListo. Se ha abierto http://localhost:$Port : pulsa «Conectar con Twitch»." -ForegroundColor Green
    } else {
        Warn "`nLa app no respondió a tiempo. Abre el acceso directo «$AppName» del escritorio."
    }
} else {
    Write-Host "`nInstalado (sin arrancar)." -ForegroundColor Green
}
