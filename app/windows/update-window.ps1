# Independent taskbar window; -Library exposes presentation logic for offline tests.
param([switch]$Library, [switch]$SmokeTest)
function Get-UpdatePresentation($value) {
    if (-not $value) { return @{ Title = 'OS3 Router - update status unavailable'; Message = 'Waiting for update progress. If this persists, open Settings to check the router.'; Mode = 'waiting' } }
    $state = [string]$value.state
    if ($state -eq 'queued' -and $value.updated -and
        ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$value.updated) -gt 120) {
        return @{ Title = 'OS3 Router - update waiting'; Message = 'The updater has not picked up this request. Open Settings to check the router. Installation is unconfirmed.'; Mode = 'failed' }
    }
    if ($state -in @('installing', 'switching') -and $value.updated -and
        ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$value.updated) -gt 1200) {
        return @{ Title = 'OS3 Router - update needs attention'; Message = 'Update progress stopped. Completion is unconfirmed. Open Settings or check service.log before retrying.'; Mode = 'failed' }
    }
    switch ($state) {
        'installed' { return @{ Title = 'OS3 Router - update complete'; Message = [string]$value.message; Mode = 'installed' } }
        'failed' { return @{ Title = 'OS3 Router - update failed'; Message = [string]$value.message; Mode = 'failed' } }
        'switching' { return @{ Title = 'OS3 Router - restarting'; Message = [string]$value.message; Mode = 'active' } }
        'installing' { return @{ Title = ('OS3 Router - installing {0}' -f $value.tag); Message = [string]$value.message; Mode = 'active' } }
        default { return @{ Title = 'OS3 Router - waiting for update'; Message = 'Waiting for update progress. Completion is unconfirmed.'; Mode = 'waiting' } }
    }
}
if ($Library) { return }
$ErrorActionPreference = 'Stop'
$HomeDir = if ($env:CODEX_OS3_HOME) { $env:CODEX_OS3_HOME } else { Join-Path $env:USERPROFILE '.codex-os3' }
$hash = [System.Security.Cryptography.SHA256]::Create()
$key = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($HomeDir).ToLowerInvariant()))).Replace('-', '')
$hash.Dispose()
$created = $false
$mutex = New-Object System.Threading.Mutex($true, ('Local\OS3RouterUpdate-' + $key), [ref]$created)
if (-not $created) { $mutex.Dispose(); return }
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class OS3UpdateTaskbar {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string id);
    [ComImport, Guid("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface ITaskbarList3 {
        void HrInit(); void AddTab(IntPtr h); void DeleteTab(IntPtr h);
        void ActivateTab(IntPtr h); void SetActiveAlt(IntPtr h);
        void MarkFullscreenWindow(IntPtr h, [MarshalAs(UnmanagedType.Bool)] bool fullscreen);
        void SetProgressValue(IntPtr h, ulong completed, ulong total);
        void SetProgressState(IntPtr h, uint state);
    }
    private static ITaskbarList3 bar;
    public static void Progress(IntPtr h, uint state) {
        if (bar == null) {
            bar = (ITaskbarList3)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("56FDF344-FD6D-11D0-958A-006097C9A090")));
            bar.HrInit();
        }
        if (state == 4) bar.SetProgressValue(h, 1, 1);
        bar.SetProgressState(h, state);
    }
}
'@
    [OS3UpdateTaskbar]::SetCurrentProcessExplicitAppUserModelID('OS3.Router.Update') | Out-Null
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'OS3 Router - updating'
    $form.ClientSize = New-Object System.Drawing.Size(720, 400)
    $form.Font = New-Object System.Drawing.Font('Arial', 15)
    $form.StartPosition = 'CenterScreen'
    $form.ShowInTaskbar = $true
    $form.MinimizeBox = $true
    $form.MaximizeBox = $false
    $form.FormBorderStyle = 'FixedSingle'
    $iconPath = Join-Path $PSScriptRoot '..\..\assets\icon.ico'
    if (Test-Path $iconPath) { $form.Icon = New-Object System.Drawing.Icon($iconPath) }
    $label = New-Object System.Windows.Forms.TextBox
    $label.SetBounds(32, 64, 656, 128)
    $label.Multiline = $true
    $label.ReadOnly = $true
    $label.ScrollBars = 'Vertical'
    $label.BorderStyle = 'None'
    $label.Font = New-Object System.Drawing.Font('Arial', 18)
    $form.Controls.Add($label)
    $progress = New-Object System.Windows.Forms.ProgressBar
    $progress.SetBounds(32, 208, 656, 24)
    $progress.Style = 'Marquee'
    $form.Controls.Add($progress)
    $note = New-Object System.Windows.Forms.Label
    $note.SetBounds(32, 248, 656, 64)
    $note.Text = 'Closing this window does not cancel the update.'
    $form.Controls.Add($note)
    $open = New-Object System.Windows.Forms.Button
    $open.SetBounds(32, 336, 260, 44)
    $open.Text = 'Open Router Settings'
    $open.add_Click({
        try {
            $launchArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Page settings' -f (Join-Path $PSScriptRoot 'open-app.ps1')
            Start-Process powershell.exe -WindowStyle Hidden -ArgumentList $launchArgs
        } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Could not open OS3 Router') | Out-Null }
    })
    $form.Controls.Add($open)
    $close = New-Object System.Windows.Forms.Button
    $close.SetBounds(568, 336, 120, 44)
    $close.Text = 'Close'
    $close.add_Click({ $form.Close() })
    $form.Controls.Add($close)
    $theme = New-Object System.Windows.Forms.Button
    $theme.SetBounds(632, 8, 56, 44)
    $theme.Text = [char]0x263C
    $theme.AccessibleName = 'toggle light and dark theme'
    $form.Controls.Add($theme)
    $script:light = $false
    function Apply-Theme {
        $bg = if ($script:light) { [Drawing.Color]::FromArgb(188, 193, 201) } else { [Drawing.Color]::Black }
        $fg = if ($script:light) { [Drawing.Color]::Black } else { [Drawing.Color]::White }
        $form.BackColor = $bg; $form.ForeColor = $fg
        foreach ($control in $form.Controls) {
            $control.BackColor = $bg; $control.ForeColor = $fg
            if ($control -is [System.Windows.Forms.Button]) {
                $control.FlatStyle = 'Flat'; $control.FlatAppearance.BorderSize = 0
            }
        }
        $open.ForeColor = $fg
        $theme.Text = if ($script:light) { [char]0x263E } else { [char]0x263C }
    }
    $theme.add_Click({ $script:light = -not $script:light; Apply-Theme })
    Apply-Theme
    $script:lastProgress = $null
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 1000
    $timer.add_Tick({
        try { $script:lastProgress = Get-Content (Join-Path $HomeDir 'update-progress.json') -Raw | ConvertFrom-Json }
        catch {} # preserve the last good state through a transient read/replace race
        $view = Get-UpdatePresentation $script:lastProgress
        $form.Text = $view.Title
        $label.Text = $view.Message
        $progress.Style = if ($view.Mode -in @('active', 'waiting')) { 'Marquee' } else { 'Continuous' }
        $progress.Value = if ($view.Mode -eq 'installed') { 100 } else { 0 }
        try {
            $taskbarState = if ($view.Mode -eq 'failed') { 4 } elseif ($view.Mode -eq 'installed') { 0 } else { 1 }
            [OS3UpdateTaskbar]::Progress($form.Handle, $taskbarState)
        } catch {} # Explorer unavailable: the window and status still work
    })
    $script:smokeError = $null
    $form.add_Shown({
        if ($SmokeTest) {
            try {
                if (-not $form.Visible -or -not $form.ShowInTaskbar -or $form.Handle -eq [IntPtr]::Zero) { throw 'Update window has no visible taskbar form' }
                $script:light = $true; Apply-Theme
                if ($form.BackColor.R -ne 188) { throw 'light theme failed' }
                $script:light = $false; Apply-Theme
                if ($form.BackColor.R -ne 0) { throw 'dark theme failed' }
                foreach ($state in 1, 4, 0) { [OS3UpdateTaskbar]::Progress($form.Handle, $state) }
                Write-Host 'Update window shown; taskbar activity/error/clear calls and both themes passed'
            } catch { $script:smokeError = $_ }
            finally { $form.Close() }
        } else { $timer.Start() }
    })
    $form.add_FormClosed({ $timer.Stop(); $timer.Dispose() })
    [System.Windows.Forms.Application]::Run($form)
    $form.Dispose()
    if ($script:smokeError) { throw $script:smokeError }
} catch {
    try { Add-Content (Join-Path $HomeDir 'tray.log') ('Update window failed: ' + $_.Exception.Message) } catch {}
    throw
} finally {
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
