# Start menu entry "OS3 Router": straight to the Edge/Chrome app window (no console flash). Used by the
# installer and, on the first run of a new version, by the router's updater.
$AppDir = Split-Path (Split-Path $PSScriptRoot)
$ErrorActionPreference = 'Stop'

$OpenApp = Join-Path $PSScriptRoot "open-app.ps1"
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "OS3 Router.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$t = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $OpenApp -Print
if ($t) { $s.TargetPath, $s.Arguments = $t -split "\|", 2 }
else { $s.TargetPath = "powershell.exe"; $s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$OpenApp`"" }
$s.IconLocation = (Join-Path $AppDir "assets\icon.ico"); $s.Description = "OS3 Router"; $s.Save()
