# Opens the OS3 Router app window: the router's own page (/app) in an Edge or Chrome app window
# (its own window and taskbar entry, no address bar). Edge is on every Windows 10/11 and signed by
# Microsoft, so there is no SmartScreen warning. Falls back to the default browser.
param([string]$Page = "", [switch]$Print)  # -Print: output "browser|arguments" for a shortcut instead
$HomeDir = if ($env:CODEX_OS3_HOME) { $env:CODEX_OS3_HOME } else { Join-Path $env:USERPROFILE ".codex-os3" }
try { $p = (Get-Content (Join-Path $HomeDir "config.json") -Raw | ConvertFrom-Json).port } catch { $p = $null }
$url = "http://localhost:$(if ($p) { $p } else { 11435 })/app$(if ($Page) { "#$Page" })"
$browsers = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
$b = $browsers | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
# its own browser profile: no first-run welcome, and the normal browser profile is never touched
$flags = "--app=$url --window-size=1100,760 --user-data-dir=`"$(Join-Path $HomeDir 'appwindow')`" --no-first-run --no-default-browser-check"
if ($Print) { if ($b) { "$b|$flags" }; return }
if ($b) { Start-Process $b -ArgumentList $flags } else { Start-Process $url }
