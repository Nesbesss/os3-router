# Runs with Windows PowerShell 5.1 in CI; no network, login or real update needed.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../app/windows/update-window.ps1') -Library
function Assert($condition, $message) { if (-not $condition) { throw $message } }
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
foreach ($state in 'installing', 'switching', 'installed', 'failed') {
    $v = Get-UpdatePresentation ([pscustomobject]@{ state = $state; tag = 'v0.9.0'; message = 'specific status'; updated = $now })
    Assert ($v.Message -eq 'specific status') ('lost message for ' + $state)
    $expected = if ($state -in @('installing', 'switching')) { 'active' } else { $state }
    Assert ($v.Mode -eq $expected) ('wrong progress mode for ' + $state)
}
$v = Get-UpdatePresentation $null
Assert ($v.Mode -eq 'waiting') 'missing status must not mean success'
$v = Get-UpdatePresentation ([pscustomobject]@{ state = 'switching'; tag = 'v0.9.0'; updated = ($now - 1201) })
Assert ($v.Mode -eq 'failed') 'stalled updates must request attention'
Assert ($v.Message -like '*unconfirmed*') 'stalled update must not claim completion'
$v = Get-UpdatePresentation ([pscustomobject]@{ state = 'installed'; message = 'confirmed'; updated = ($now - 99999) })
Assert ($v.Mode -eq 'installed') 'confirmed completion must survive a later reopen'
Write-Host 'Windows update presentation tests passed'

$v = Get-UpdatePresentation ([pscustomobject]@{ state = 'queued'; tag = 'v0.9.0'; updated = ($now - 121) })
Assert ($v.Mode -eq 'failed') 'old queue should request attention'
Assert ($v.Message -like '*not picked up*') 'queued status must not claim installation'


# Execute the installer's actual helper definitions without running installation.
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path (Join-Path $PSScriptRoot '../install.ps1')), [ref]$null, [ref]$null)
foreach ($name in 'Test-InstallHealth', 'Restore-Previous') {
    $fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Assert ($null -ne $fn) ('installer helper missing: ' + $name)
    Invoke-Expression $fn.Extent.Text
}
$health = [pscustomobject]@{ status = 'ok'; pid = 20; version = '0.9.0' }
Assert (Test-InstallHealth $health '0.9.0' 10 $true) 'replacement worker must be accepted'
Assert (-not (Test-InstallHealth $health '0.9.0' 20 $true)) 'old PID must not confirm an upgrade'
Assert (-not (Test-InstallHealth $health '0.8.0' 10 $true)) 'wrong version must not confirm an upgrade'
$health.status = 'error'
Assert (-not (Test-InstallHealth $health '0.9.0' 10 $true)) 'unhealthy response must not confirm an upgrade'
$health.status = 'ok'; $health.pid = $null
Assert (-not (Test-InstallHealth $health '0.9.0' 10 $true)) 'missing PID must not confirm an upgrade'
$HomeDir = Join-Path $env:TEMP ('os3-rollback-test-' + [guid]::NewGuid())
$AppDir = Join-Path $HomeDir 'app'
$script:Swapped = $true
try {
    New-Item -ItemType Directory -Force $AppDir, ($AppDir + '.old') | Out-Null
    Set-Content (Join-Path ($AppDir + '.old') 'keep.txt') 'previous'
    Set-Content (Join-Path $AppDir 'keep.txt') 'broken'
    Set-Content (Join-Path $AppDir 'introduced.txt') 'new'
    Restore-Previous
    Assert ((Get-Content (Join-Path $AppDir 'keep.txt')) -eq 'previous') 'rollback did not restore old files'
    Assert (-not (Test-Path (Join-Path $AppDir 'introduced.txt'))) 'rollback retained new files'
    Assert (Test-Path (Join-Path $HomeDir 'reload.request')) 'rollback did not request a reload'
} finally { Remove-Item $HomeDir -Recurse -Force }
Write-Host 'Windows installer health and rollback tests passed'
