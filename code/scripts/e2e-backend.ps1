# E2E backend: isolated workspace, migrations, seeds, then serve on the E2E port.
# Playwright owns this process (it is the webServer command), so the app is
# stopped when the test run ends.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Push-Location $root
try {
    $port = if ($env:PERSONAL_AGENT_E2E_PORT) { [int]$env:PERSONAL_AGENT_E2E_PORT } else { 8123 }
    $dataDir = [IO.Path]::GetFullPath((Join-Path $root '.cache\e2e-data'))
    $cacheRoot = [IO.Path]::GetFullPath((Join-Path $root '.cache'))
    if ((Split-Path -Parent $dataDir) -ne $cacheRoot) {
        throw 'E2E data path must be a child of the workspace cache directory'
    }
    if (Test-Path -LiteralPath $dataDir) {
        if ((Get-Item -LiteralPath $dataDir).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'E2E data directory must not be a junction or symbolic link'
        }
        Remove-Item -LiteralPath $dataDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $dataDir 'db') | Out-Null

    $dbPath = (Join-Path $dataDir 'db\personal-agent.db').Replace('\', '/')
    $env:PERSONAL_AGENT_DATA_DIR = $dataDir
    $env:PERSONAL_AGENT_DB_URL = "sqlite+aiosqlite:///$dbPath"
    $env:PERSONAL_AGENT_MODE = 'mock'
    $env:PERSONAL_AGENT_HOST = '127.0.0.1'
    $env:PERSONAL_AGENT_PORT = "$port"
    $env:UV_CACHE_DIR = Join-Path $root '.cache\uv'

    & uv run --project code/backend alembic -c code/backend/alembic.ini upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'migration failed' }

    & uv run --project code/backend python -m personal_agent.bootstrap.seed
    if ($LASTEXITCODE -ne 0) { throw 'seed failed' }

    & uv run --project code/backend --python 3.12 python code/backend/tests/e2e_server.py
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
