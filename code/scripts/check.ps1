# Personal-Agent 必跑检查入口（T007）
# 用法（仓库根）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1 -E2E
# 默认离线；任何一步失败整体返回非零，且不把未运行项报告为通过。

[CmdletBinding()]
param(
    [switch]$E2E,
    [switch]$SkipFrontend,
    [switch]$SkipPolicy
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $root 'AGENTS.md'))) {
    $root = (Get-Location).Path
}
Push-Location $root

$script:failures = New-Object System.Collections.Generic.List[string]
$script:ran = New-Object System.Collections.Generic.List[string]

function Invoke-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    try {
        & $Action
        $code = $LASTEXITCODE
    }
    catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        $code = 1
    }
    if ($null -eq $code) { $code = 0 }
    $script:ran.Add($Name) | Out-Null
    if ($code -ne 0) {
        Write-Host "FAILED ($code): $Name" -ForegroundColor Red
        $script:failures.Add($Name) | Out-Null
    }
    else {
        Write-Host "passed: $Name" -ForegroundColor Green
    }
}

Write-Host "Personal-Agent checks - root: $root"
Write-Host ("powershell {0}" -f $PSVersionTable.PSVersion)

Invoke-Check 'ruff (backend)' {
    & uv run --project code/backend ruff check code/backend
}
Invoke-Check 'mypy (backend src)' {
    & uv run --project code/backend mypy code/backend/src
}
Invoke-Check 'pytest offline (unit/contract/integration)' {
    & uv run --project code/backend python -m pytest code/backend/tests/unit code/backend/tests/contract code/backend/tests/integration -q
}

if (-not $SkipFrontend) {
    Invoke-Check 'vue-tsc (frontend)' {
        & npm run typecheck --prefix code/frontend
    }
    Invoke-Check 'vitest (frontend)' {
        & npm run test --prefix code/frontend -- --run
    }
}

if (-not $SkipPolicy) {
    Invoke-Check 'tsc (runtime-policy)' {
        & npm run typecheck --prefix code/runtime-policy
    }
    Invoke-Check 'vitest (runtime-policy)' {
        & npm run test --prefix code/runtime-policy -- --run
    }
}

if ($E2E) {
    Invoke-Check 'playwright e2e' {
        & npm run test:e2e --prefix code/frontend
    }
}

Write-Host ""
Write-Host "=== lock fingerprints (SHA-256) ===" -ForegroundColor Cyan
foreach ($file in @('code/backend/uv.lock', 'code/frontend/package-lock.json', 'code/runtime-policy/package-lock.json')) {
    if (Test-Path $file) {
        $hash = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
        Write-Host ("{0}  {1}" -f $hash, $file)
    }
    else {
        Write-Host "MISSING lock file: $file" -ForegroundColor Red
        $script:failures.Add("missing $file") | Out-Null
    }
}

Write-Host ""
Write-Host ("checks run: {0}" -f $script:ran.Count)
if ($script:failures.Count -gt 0) {
    Write-Host ("FAILED: {0}" -f ($script:failures -join '; ')) -ForegroundColor Red
    Pop-Location
    exit 1
}
Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
Pop-Location
exit 0
