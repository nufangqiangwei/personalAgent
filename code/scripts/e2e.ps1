# E2E runner: build the same-origin frontend, then let Playwright start the
# seeded backend (code/scripts/e2e-backend.ps1) and run the specs.
param(
    [string]$Grep = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Push-Location $root
try {
    Write-Host '=== build frontend ==='
    & npm run build --prefix code/frontend
    if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }

    $env:PERSONAL_AGENT_E2E_PORT = if ($env:PERSONAL_AGENT_E2E_PORT) { $env:PERSONAL_AGENT_E2E_PORT } else { '8123' }
    $env:PERSONAL_AGENT_E2E_NO_SERVER = ''

    $npmArgs = @('run', 'test:e2e', '--prefix', 'code/frontend')
    if ($Grep -ne '') {
        $npmArgs += '--'
        $npmArgs += '--grep'
        $npmArgs += $Grep
    }
    & npm @npmArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
