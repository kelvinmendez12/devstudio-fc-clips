# Quita la tarea programada y el acceso directo. No borra tus clips, tus VODs ni tu calibración.
$ErrorActionPreference = "Continue"
foreach ($name in @("DevStudio FC Clips", "RKM Clips")) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "rkm_clips" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
foreach ($name in @("DevStudio FC Clips.lnk", "RKM Clips.lnk")) {
    Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) $name) -ErrorAction SilentlyContinue
}
Write-Host "DevStudio FC Clips desinstalado. Tus clips (carpeta Vídeos) y tu calibración se conservan."
Write-Host "Si quieres borrar también los datos: %LOCALAPPDATA%\DevStudioFCClips"
