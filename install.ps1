# os3-router installer for Windows (beta: CI-tested; not yet tested with a real rabbit-agent on Windows)
#   irm https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.ps1 | iex
# Options (when run as a file): -Uninstall [-Purge]  -NoTray  -NoWait  -Port N
# Env: CODEX_OS3_SRC=<local checkout>, CODEX_OS3_REF=<branch|tag> (default: the latest release)
param([switch]$Uninstall, [switch]$Purge, [switch]$NoTray, [switch]$NoWait, [int]$Port = 0)
$ErrorActionPreference = "Stop"

$Repo = "Nesbesss/os3-router"
$Ref = if ($env:CODEX_OS3_REF) { $env:CODEX_OS3_REF } else { "" }  # empty: the latest release (main only if GitHub cannot say which it is)
$HomeDir = if ($env:CODEX_OS3_HOME) { $env:CODEX_OS3_HOME } else { Join-Path $env:USERPROFILE ".codex-os3" }
$AppDir = Join-Path $HomeDir "app"
$TaskName = "codex-os3 router"
$TrayTask = "codex-os3 tray"

# (ASCII marks: the classic Windows console font has no check mark)
function Ok($m) { Write-Host "     [ok] " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn($m) { Write-Host "     [!]  " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Note($m) { Write-Host "          $m" -ForegroundColor DarkGray }
$Step = "starting"; $StepN = 0; $T0 = Get-Date
function Step($m) {
    $script:Step = $m; $script:StepN++
    Write-Host ""; Write-Host "  $script:StepN/7  " -ForegroundColor Cyan -NoNewline; Write-Host $m -ForegroundColor White
}
$Swapped = $false
# What a person helping needs to see, appended to the log when an install stops (nothing here leaves the machine)
function Save-Diagnostics {
    $ErrorActionPreference = "Continue"  # (this function only: a native program's stderr must not end the collecting)
    try {
        $log = Join-Path $HomeDir "install.log"
        $lines = @("--- diagnostics after the failure (step: $script:Step)", "Windows $([Environment]::OSVersion.Version) PowerShell $($PSVersionTable.PSVersion) $env:PROCESSOR_ARCHITECTURE")
        foreach ($c in "python", "py", "codex", "claude", "node") {
            $g = Get-Command $c -ErrorAction SilentlyContinue | Select-Object -First 1
            $lines += "$c = $(if ($g) { $g.Source } else { 'not found' })"
        }
        $lines += "PATH = $env:PATH"
        if ($Py -and (Test-Path (Join-Path $AppDir "codex_os3\__init__.py"))) {
            Push-Location $AppDir
            $lines += @(& $Py -m codex_os3 doctor 2>&1 | ForEach-Object { "$_" })
            Pop-Location
        }
        $lines += @(Get-Content (Join-Path $HomeDir "service.log") -Tail 40 -ErrorAction SilentlyContinue)
        Add-Content -Path $log -Value $lines
    } catch {}
}
# A failed upgrade must not leave the machine without a router: put the previous copy back
function Restore-Previous {
    $old = "$AppDir.old"
    if ($script:Swapped -and (Test-Path $old)) {
        try {
            Copy-Item (Join-Path $old "*") $AppDir -Recurse -Force
            Write-Host "  The previous version was put back, so the router you had keeps working." -ForegroundColor DarkGray
        } catch {}
    }
}
function Die($m) {
    Write-Host "  [x]  $m" -ForegroundColor Red
    Write-Host ""
    Write-Host "  The install stopped during: $script:Step" -ForegroundColor Red
    Write-Host "  - Running the same install command again is safe: it continues where it can."
    Write-Host "  - Still stuck? Share $HomeDir\install.log in the os3-router Discord, or open an issue:"
    Write-Host "    https://github.com/$Repo/issues/new"
    Restore-Previous
    try { Stop-Transcript | Out-Null } catch {}
    Save-Diagnostics
    # run as "irm | iex" in a console, "exit" closes the window with the message: wait for a key first
    if (-not $NoWait -and [Environment]::UserInteractive) { try { Read-Host "  Press Enter to close" | Out-Null } catch {} }
    exit 1
}
function Latest-Release {
    try { $t = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing -TimeoutSec 15).tag_name; if ($t -match '^v\d+\.\d+\.\d+$') { return $t } } catch {}
    try {  # the API's hourly quota per address can be used up: the public "latest" link is not limited
        $r = Invoke-WebRequest "https://github.com/$Repo/releases/latest" -UseBasicParsing -Method Head -TimeoutSec 15
        $u = if ($r.BaseResponse.ResponseUri) { $r.BaseResponse.ResponseUri.AbsoluteUri } else { $r.BaseResponse.RequestMessage.RequestUri.AbsoluteUri }
        if ($u -match '/releases/tag/(v\d+\.\d+\.\d+)$') { return $Matches[1] }
    } catch {}
    return $null
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
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $TaskName -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $AppDir, "$AppDir.old", (Join-Path $HomeDir "start-router.cmd") -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path ([Environment]::GetFolderPath("Programs")) "OS3 Router.lnk") -ErrorAction SilentlyContinue
    if ($Purge) { Remove-Item -Recurse -Force $HomeDir -ErrorAction SilentlyContinue; Ok "removed all data" }
    Ok "uninstalled"; exit 0
}

New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
try { Start-Transcript -Path (Join-Path $HomeDir "install.log") -Force | Out-Null } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12  # PowerShell 5.1
Write-Host ""
Write-Host "  OS3 Router  " -ForegroundColor Cyan -NoNewline; Write-Host "installer for Windows (beta)" -ForegroundColor DarkGray
Write-Host "  Your ChatGPT (Codex) subscription as the brain of rabbit OS3. Takes 1-3 minutes." -ForegroundColor DarkGray

try {
Step "Checking this machine"
try { Invoke-WebRequest "https://github.com" -UseBasicParsing -TimeoutSec 15 -Method Head | Out-Null }
catch { Die "can't reach github.com: check the internet connection (or a proxy / firewall) and re-run" }
Ok "Windows $([Environment]::OSVersion.Version) - $env:PROCESSOR_ARCHITECTURE - online"
if (Test-Path (Join-Path $env:USERPROFILE ".rabbit-agent")) { Ok "rabbit OS3 node found on this machine" }
else {
    Warn "no rabbit OS3 node on this machine yet: add this machine in OS3 (Settings -> add device) too;"
    Note "the router must run on the same machine you pick as the LLM device in OS3"
}

Step "Python"

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
    Note "no Python 3.9+ here: getting a private one just for the router (1-2 minutes)"
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
Ok "Python  $Py"

Step "Codex CLI"

# --- codex cli -------------------------------------------------------------------------
# -CommandType Application: codex.cmd/.exe, not the codex.ps1 shim (Windows can't start a .ps1
# directly: "[WinError 193] not a valid Win32 application"); the router then picks npm's codex.exe
$Codex = (Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$Standalone = Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"
if (-not $Codex -and (Test-Path $Standalone)) { $Codex = $Standalone }  # installed, not on PATH in this window yet
if (-not $Codex) {
    # OpenAI's own standalone installer: no Node.js needed
    Note "installing it with OpenAI's installer"
    $env:CODEX_NON_INTERACTIVE = "1"
    if (-not (Run-Installer "https://github.com/openai/codex/releases/latest/download/install.ps1" "codex") -or -not (Test-Path $Standalone)) {
        Die "could not install the Codex CLI: check the internet connection and re-run"
    }
    $Codex = $Standalone
}
Ok "Codex CLI  $Codex"

Step "ChatGPT sign-in"
# Windows PowerShell 5.1 turns any native stderr output into a terminating NativeCommandError while
# $ErrorActionPreference is "Stop", even when the command exits 0 ("Logged in using ChatGPT" goes to
# stderr). So run the status check with "Continue" and judge only by its exit code.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { & $Codex login status *> $null; $loginStatus = $LASTEXITCODE } finally { $ErrorActionPreference = $prevEap }
if ($loginStatus -ne 0) {
    Note "sign in with the ChatGPT account whose plan the router should use"
    $ErrorActionPreference = "Continue"
    try {
        if ($env:SSH_CONNECTION) { & $Codex login --device-auth } else { & $Codex login }  # over SSH: a link + code instead of a browser
        & $Codex login status *> $null; $loginStatus = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevEap }
    # not fatal: someone who only uses Claude cancels this on purpose. The app's setup asks which one you use and
    # checks only that one; ChatGPT users see the same problem there with the fix.
    if ($loginStatus -ne 0) { Warn "Codex isn't signed in (cancelled or timed out)"; Note "fine if you will only use Claude; otherwise run: codex login" }
}
if ($loginStatus -eq 0) { Ok "signed in to ChatGPT" }


# --- code ------------------------------------------------------------------------------
Step "OS3 Router"
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$New = Join-Path $HomeDir "app.new"
Remove-Item -Recurse -Force $New -ErrorAction SilentlyContinue
if ($env:CODEX_OS3_SRC) {
    Copy-Item -Recurse $env:CODEX_OS3_SRC $New
} else {
    if (-not $Ref) {  # a fresh install gets what updates would give it: a tested release, not main
        $Ref = Latest-Release
        if (-not $Ref) { $Ref = "main"; Warn "could not find the latest release: installing the newest code (main)" }
    }
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
$Old = "$AppDir.old"
Remove-Item -Recurse -Force $Old -ErrorAction SilentlyContinue
if (Test-Path $AppDir) {
    # over the top, like the router's own updater: the running router has this folder as its working directory,
    # so Windows will not delete or rename it. The copy is the way back if the new version does not start.
    Copy-Item -Recurse $AppDir $Old
    $Swapped = $true
    Copy-Item (Join-Path $New "*") $AppDir -Recurse -Force
    Remove-Item -Recurse -Force $New
} else {
    Move-Item $New $AppDir
}
Ok "os3-router  $AppDir"

Push-Location $AppDir
$env:CODEX_OS3_HOME = $HomeDir
# a port the router can really listen on (Windows reserves ranges), and where Claude Code is (the service's PATH is not this one)
if ($Port) { & $Py -m codex_os3 preflight $Port } else { & $Py -m codex_os3 preflight }
if ($LASTEXITCODE -ne 0) { Die "no usable port for the router: pick another with -Port" }
& $Py -c "from codex_os3 import config; config.save({'codex_bin': r'$Codex'}); config.ensure_key()"
# this installer sets up the matching tray itself; the router only restarts it on later updates
& $Py -c "from codex_os3 import store, __version__; store.kv_set('apps_version', __version__)"
# "what's new" popup: after an upgrade, everything since the old version; nothing on a fresh install
& $Py -c "from codex_os3 import store, __version__; store.kv_set('whatsnew_seen', '$OldVersion' or __version__)"
$Port = [int](& $Py -c "from codex_os3 import config; print(config.load()['port'])")

# --- service: a Task Scheduler task at logon, restarted if it stops ---------------------
Step "Background service"
if ($Running) {
    & $Py -m codex_os3 reload | Out-Null; Ok "upgraded (Windows reload has a ~1 s gap)"
} else {
    [Environment]::SetEnvironmentVariable("CODEX_OS3_HOME", $HomeDir, "User")
    try {
        $action = New-ScheduledTaskAction -Execute $PyW -Argument "-m codex_os3 serve" -WorkingDirectory $AppDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Ok "scheduled task '$TaskName' (starts at logon, restarts if it stops)"
    } catch {
        # some company PCs do not let users create tasks: start it at every logon from the user's own Run key instead (no admin needed)
        Warn "Task Scheduler refused ($($_.Exception.Message)): using your Windows startup list instead"
        $cmd = Join-Path $HomeDir "start-router.cmd"
        Set-Content -Path $cmd -Encoding ASCII -Value "@echo off`r`ncd /d `"$AppDir`"`r`nset CODEX_OS3_HOME=$HomeDir`r`nstart `"`" /b `"$PyW`" -m codex_os3 serve"
        Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $TaskName -Value "`"$cmd`""
        Start-Process -FilePath $cmd -WindowStyle Hidden
        Ok "starts at every logon (startup list); it will not restart itself if it stops"
    }
}
$up = $false
for ($i = 0; $i -lt 30 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null; $up = $true } catch { Start-Sleep 1 }
}
if (-not $up) {
    Get-Content (Join-Path $HomeDir "service.log") -Tail 25 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
    Die "router did not start (the lines above are the end of $HomeDir\service.log)"
}
Ok "router running on http://127.0.0.1:$Port"
Remove-Item -Recurse -Force "$AppDir.old" -ErrorAction SilentlyContinue; $Swapped = $false  # the new version runs: no way back needed
# what the running service sees (not this window): Codex, the sign-in, Claude; plus one real request when someone is watching
$vflag = @()
if (-not $NoWait) { Note "testing with a real request (up to a minute)"; $vflag = @("--selftest") }
& $Py -m codex_os3 verify @vflag
if ($LASTEXITCODE -ne 0) { Warn "something above needs a look; the app's Help page can walk you through it" }

Step "App and OS3 connection"

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
Write-Host ""
Write-Host "  Installed in $([int]((Get-Date) - $T0).TotalSeconds)s" -ForegroundColor Green
Write-Host ""
Write-Host "  Last step: " -NoNewline; Write-Host "save the connection in OS3 and send it a message. Waiting for OS3... (Ctrl-C to skip)" -ForegroundColor DarkGray
& $Py -m codex_os3 wait-for-os3 1800 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "OS3 is connected — you're done" } else { Warn "no request from OS3 yet; the dashboard shows when it connects" }
Pop-Location
try { Stop-Transcript | Out-Null } catch {}
} catch {
    Die "$($_.Exception.Message)"
}
