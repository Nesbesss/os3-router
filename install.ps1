# os3-router installer for Windows (beta: CI-tested; not yet tested with a real rabbit-agent on Windows)
#   irm https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.ps1 | iex
# Options (when run as a file): -Uninstall [-Purge]  -NoTray  -NoWait  -Port N
# Env: CODEX_OS3_SRC=<local checkout>, CODEX_OS3_REF=<branch|tag>
param([switch]$Uninstall, [switch]$Purge, [switch]$NoTray, [switch]$NoWait, [int]$Port = 0)
$ErrorActionPreference = "Stop"

$Repo = "Nesbesss/os3-router"
$Ref = if ($env:CODEX_OS3_REF) { $env:CODEX_OS3_REF } else { "main" }
$HomeDir = if ($env:CODEX_OS3_HOME) { $env:CODEX_OS3_HOME } else { Join-Path $env:USERPROFILE ".codex-os3" }
$AppDir = Join-Path $HomeDir "app"
$TaskName = "codex-os3 router"
$TrayTask = "codex-os3 tray"

function Ok($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
$Step = "starting"
function Step($m) { $script:Step = $m; Write-Host $m -ForegroundColor White }
function Die($m) {
    Write-Host "  [x]  $m" -ForegroundColor Red
    Write-Host ""
    Write-Host "  The install stopped during: $script:Step" -ForegroundColor Red
    Write-Host "  - Running the same install command again is safe: it continues where it can."
    Write-Host "  - Still stuck? Share $HomeDir\install.log in the os3-router Discord, or open an issue:"
    Write-Host "    https://github.com/$Repo/issues/new"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
# Runs a downloaded installer script in its own PowerShell, so its "exit" can't end this one
function Run-Installer($url, $name) {
    $f = Join-Path $env:TEMP "os3-$name.ps1"
    try { Invoke-WebRequest $url -OutFile $f -UseBasicParsing } catch { return $false }
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $f *> $null; return $LASTEXITCODE -eq 0 }
    finally { $ErrorActionPreference = $prev; Remove-Item $f -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    foreach ($t in $TaskName, $TrayTask) { schtasks /End /TN $t 2>$null | Out-Null; schtasks /Delete /TN $t /F 2>$null | Out-Null }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*codex_os3*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $AppDir -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path ([Environment]::GetFolderPath("Programs")) "OS3 Router.lnk") -ErrorAction SilentlyContinue
    if ($Purge) { Remove-Item -Recurse -Force $HomeDir -ErrorAction SilentlyContinue; Ok "removed all data" }
    Ok "uninstalled"; exit 0
}

New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
try { Start-Transcript -Path (Join-Path $HomeDir "install.log") -Force | Out-Null } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12  # PowerShell 5.1
Write-Host "os3-router installer (Windows, beta)" -ForegroundColor White

# --- python --------------------------------------------------------------------------
$Py = $null
foreach ($c in "python", "python3", "py") {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd -or $cmd.Source -like "*WindowsApps*") { continue }  # skip the Microsoft Store stub
    $extra = @()
    if ($c -eq "py") { $extra = @("-3") }
    try { $out = @(& $cmd.Source @extra -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1]); print(sys.executable)" 2>$null) }
    catch { continue }
    if ($out.Count -ge 2 -and [int]$out[0] -ge 309) { $Py = $out[1].Trim(); break }
}
$Uv = Join-Path $HomeDir "bin\uv.exe"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $HomeDir "python"
if (-not $Py -and -not (Test-Path $Uv)) {  # no Python 3.9+: a private one for the router (nothing else changes)
    Step "Getting Python for the router (no Python 3.9+ on this machine)"
    $env:UV_INSTALL_DIR = Join-Path $HomeDir "bin"; $env:UV_NO_MODIFY_PATH = "1"
    if (-not (Run-Installer "https://astral.sh/uv/install.ps1" "uv")) { Die "could not download uv (the Python installer): check the internet connection and re-run" }
}
if (-not $Py -and (Test-Path $Uv)) {
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $Uv python install 3.12 *> $null; $Py = (& $Uv python find --managed-python 3.12 2>$null | Select-Object -First 1) } finally { $ErrorActionPreference = $prevEap }
}
if (-not $Py) { Die "could not get Python 3.12: check the internet connection and re-run (or install Python from https://www.python.org/downloads/ with 'Add to PATH' ticked)" }
$PyW = Join-Path (Split-Path $Py) "pythonw.exe"
if (-not (Test-Path $PyW)) { $PyW = $Py }
Ok "python: $Py"

# --- codex cli -------------------------------------------------------------------------
# -CommandType Application: codex.cmd/.exe, not the codex.ps1 shim (Windows can't start a .ps1
# directly: "[WinError 193] not a valid Win32 application"); the router then picks npm's codex.exe
$Codex = (Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$Standalone = Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"
if (-not $Codex -and (Test-Path $Standalone)) { $Codex = $Standalone }  # installed, not on PATH in this window yet
if (-not $Codex) {
    # OpenAI's own standalone installer: no Node.js needed
    Step "Installing the Codex CLI"
    $env:CODEX_NON_INTERACTIVE = "1"
    if (-not (Run-Installer "https://github.com/openai/codex/releases/latest/download/install.ps1" "codex") -or -not (Test-Path $Standalone)) {
        Die "could not install the Codex CLI: check the internet connection and re-run"
    }
    $Codex = $Standalone
}
Ok "codex: $Codex"
# Windows PowerShell 5.1 turns any native stderr output into a terminating NativeCommandError while
# $ErrorActionPreference is "Stop", even when the command exits 0 ("Logged in using ChatGPT" goes to
# stderr). So run the status check with "Continue" and judge only by its exit code.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { & $Codex login status *> $null; $loginStatus = $LASTEXITCODE } finally { $ErrorActionPreference = $prevEap }
if ($loginStatus -ne 0) {
    Step "Sign in to Codex with your ChatGPT account"
    $ErrorActionPreference = "Continue"
    try {
        if ($env:SSH_CONNECTION) { & $Codex login --device-auth } else { & $Codex login }  # over SSH: a link + code instead of a browser
        & $Codex login status *> $null; $loginStatus = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevEap }
    if ($loginStatus -ne 0) { Die "Codex isn't signed in (the sign-in was cancelled or timed out): re-run to try again" }
}
Ok "codex is signed in"

# --- rabbit-agent ----------------------------------------------------------------------
if (Test-Path (Join-Path $env:USERPROFILE ".rabbit-agent")) { Ok "rabbit-agent found on this machine" }
else { Warn "no rabbit OS3 node on this machine yet — install it from OS3 first; the router must run on the machine you pick as the LLM device" }

# --- code ------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$New = Join-Path $HomeDir "app.new"
Remove-Item -Recurse -Force $New -ErrorAction SilentlyContinue
if ($env:CODEX_OS3_SRC) {
    Copy-Item -Recurse $env:CODEX_OS3_SRC $New
} else {
    $Zip = Join-Path $HomeDir "src.zip"
    try { Invoke-WebRequest "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $Zip -UseBasicParsing }
    catch {
        if (Get-Command gh -ErrorAction SilentlyContinue) { gh api "repos/$Repo/zipball/$Ref" > $Zip } else { Die "could not download $Repo@$Ref" }
    }
    $Tmp = Join-Path $HomeDir "src.tmp"; Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    Expand-Archive $Zip $Tmp; Move-Item (Get-ChildItem $Tmp | Select-Object -First 1).FullName $New
    Remove-Item -Recurse -Force $Tmp, $Zip
}
if (-not (Test-Path (Join-Path $New "codex_os3\__init__.py"))) { Die "download looks incomplete" }
$Running = $false
try { Invoke-RestMethod "http://127.0.0.1:$(if ($Port) { $Port } else { 11435 })/health" -TimeoutSec 2 | Out-Null; $Running = $true } catch {}
$OldVersion = ""
try { $OldVersion = ([regex]'__version__ = "([^"]+)"').Match((Get-Content (Join-Path $AppDir "codex_os3\__init__.py") -Raw)).Groups[1].Value } catch {}
Remove-Item -Recurse -Force $AppDir -ErrorAction SilentlyContinue
Move-Item $New $AppDir
Ok "installed to $AppDir"

Push-Location $AppDir
$env:CODEX_OS3_HOME = $HomeDir
if ($Port) { & $Py -c "from codex_os3 import config; config.save({'port': $Port})" }
& $Py -c "from codex_os3 import config; config.save({'codex_bin': r'$Codex'}); config.ensure_key()"
# this installer sets up the matching tray itself; the router only restarts it on later updates
& $Py -c "from codex_os3 import store, __version__; store.kv_set('apps_version', __version__)"
# "what's new" popup: after an upgrade, everything since the old version; nothing on a fresh install
& $Py -c "from codex_os3 import store, __version__; store.kv_set('whatsnew_seen', '$OldVersion' or __version__)"
$Port = [int](& $Py -c "from codex_os3 import config; print(config.load()['port'])")

# --- service: a Task Scheduler task at logon, restarted if it stops ---------------------
if ($Running) {
    & $Py -m codex_os3 reload | Out-Null; Ok "upgraded (Windows reload has a ~1 s gap)"
} else {
    $action = New-ScheduledTaskAction -Execute $PyW -Argument "-m codex_os3 serve" -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    [Environment]::SetEnvironmentVariable("CODEX_OS3_HOME", $HomeDir, "User")
    Start-ScheduledTask -TaskName $TaskName
    Ok "scheduled task '$TaskName' (starts at logon, restarts if it stops)"
}
$up = $false
for ($i = 0; $i -lt 30 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null; $up = $true } catch { Start-Sleep 1 }
}
if (-not $up) {
    Get-Content (Join-Path $HomeDir "service.log") -Tail 25 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
    Die "router did not start (the lines above are the end of $HomeDir\service.log)"
}
Ok "router answering on http://127.0.0.1:$Port"

# --- tray app --------------------------------------------------------------------------
if (-not $NoTray) {
    $tray = Join-Path $AppDir "app\windows\tray.ps1"
    $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$tray`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $trigger.Delay = "PT20S"  # let the desktop finish loading: a tray started the instant of logon can die with 0xc0000142
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive  # the tray needs the user's desktop
    $traySettings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TrayTask -Action $a -Trigger $trigger -Principal $principal -Settings $traySettings -Force | Out-Null
    try { Start-ScheduledTask -TaskName $TrayTask; Ok "tray icon installed (bottom-right, next to the clock)" }
    catch { Warn "tray icon installed, but could not start it now (it starts at your next logon): $_" }
}

# --- app window: Start menu entry ------------------------------------------------------
$OpenApp = Join-Path $AppDir "app\windows\open-app.ps1"
try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppDir "app\windows\shortcut.ps1"); Ok "Start menu: OS3 Router" }
catch { Warn "could not create the Start menu entry: $_" }

& $Py -m codex_os3 setup-info
if ($NoWait) { Pop-Location; try { Stop-Transcript | Out-Null } catch {}; exit 0 }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $OpenApp -Page setup
Write-Host "Waiting for OS3 to connect… (save the connection in OS3 and send it a message; Ctrl-C to skip)"
& $Py -m codex_os3 wait-for-os3 1800 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "OS3 is connected — you're done" } else { Warn "no request from OS3 yet; the dashboard shows when it connects" }
Pop-Location
try { Stop-Transcript | Out-Null } catch {}
