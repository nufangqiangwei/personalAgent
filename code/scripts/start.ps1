# Personal-Agent production-style launcher (T101).
#
# One instance, one worker, no reload. Before serving it verifies the pinned
# toolchain, syncs the frozen Python environment, checks the JavaScript installs
# and the pinned Playwright browser, applies migrations, imports the backend and
# runs the offline startup diagnostics. It then serves the API and, when
# code/frontend/dist exists, the built same-origin UI.
#
# Usage (repository root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -Port 8123 -DataDir .cache/start-data
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -CheckOnly
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -InstallBrowsers
#
# Exit codes: 0 = normal stop, 1 = preflight/self-check failure, 2 = another
# instance already owns this data directory (the backend refuses a second one).
# No secret is read or printed: credentials stay in the environment/credential
# store and are only consumed by the live model gateway after the adoption gate.

[CmdletBinding()]
param(
    [int]$Port = 0,
    [Alias('Host')]
    [string]$BindHost = '',
    [string]$Mode = '',
    [string]$DataDir = '',
    [switch]$CheckOnly,
    [switch]$InstallBrowsers,
    [switch]$NoSync
)

$ErrorActionPreference = 'Stop'
$script:repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $script:repoRoot 'AGENTS.md'))) {
    $script:repoRoot = (Get-Location).Path
}
$script:failExitCode = 1

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=== ' + $Title + ' ===') -ForegroundColor Cyan
}

function Write-Pass {
    param([string]$Message)
    Write-Host ('  OK   ' + $Message) -ForegroundColor Green
}

function Write-Note {
    param([string]$Message)
    Write-Host ('  ..   ' + $Message)
}

function Write-WarningLine {
    param([string]$Message)
    Write-Host ('  WARN ' + $Message) -ForegroundColor Yellow
}

function Stop-WithError {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [string[]]$Hints = @(),
        [int]$ExitCode = 1
    )
    Write-Host ''
    Write-Host ('ERROR: ' + $Message) -ForegroundColor Red
    foreach ($hint in $Hints) {
        Write-Host ('  -> ' + $hint) -ForegroundColor Yellow
    }
    $script:failExitCode = $ExitCode
    $exception = New-Object System.InvalidOperationException($Message)
    $exception.Data['StartupReported'] = $true
    throw $exception
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string[]]$Hints = @()
    )
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Stop-WithError ("{0} failed (exit {1})." -f $Label, $code) $Hints
    }
}

function Get-UvRunArguments {
    param([string[]]$CommandArguments = @())
    # --no-sync: every later command reuses exactly the environment created by
    # the explicit frozen sync above; nothing is resolved or downloaded twice.
    return @('run', '--no-sync', '--project', 'code/backend', '--python', '3.12') + $CommandArguments
}

function Test-InstanceLockHeld {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return $false
    }
    $stream = $null
    try {
        # The backend holds an OS byte-range lock on this file. Opening it with
        # no sharing is refused only while that lock is alive; a stale file from
        # a crashed process stays openable.
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        return $false
    }
    catch [System.IO.IOException] {
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $stream) {
            $stream.Close()
        }
    }
}

function Test-TcpPortAvailable {
    param([string]$Address, [int]$LocalPort)
    $listener = $null
    try {
        $parsed = $null
        if ($Address -eq 'localhost') {
            $parsed = [System.Net.IPAddress]::Loopback
        }
        elseif (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
            # A host name cannot be probed safely here; uvicorn reports the bind error.
            return $true
        }
        $listener = New-Object System.Net.Sockets.TcpListener($parsed, $LocalPort)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            try { $listener.Stop() } catch { }
        }
    }
}

function Get-PlaywrightBrowserState {
    param([string]$UvExe)
    $state = New-Object PSObject -Property @{
        Revision = $null
        ChromiumPath = $null
        Missing = @()
    }
    $global:LASTEXITCODE = 0
    $dryRunArguments = Get-UvRunArguments @('python', '-m', 'playwright', 'install', '--dry-run', 'chromium')
    $output = & $UvExe @dryRunArguments
    if ($LASTEXITCODE -ne 0) {
        return $state
    }
    $locations = @()
    $currentRevision = $null
    $chromiumPath = $null
    foreach ($item in $output) {
        $line = [string]$item
        if ($line -match 'playwright chromium v(\d+)') {
            $currentRevision = $Matches[1]
            $state.Revision = $Matches[1]
        }
        elseif ($line -match '^\s*Install location:\s*(.+?)\s*$') {
            $location = $Matches[1]
            $locations += $location
            if ($null -eq $chromiumPath -and $location -match 'chromium-\d+$') {
                $chromiumPath = $location
            }
        }
    }
    $state.ChromiumPath = $chromiumPath
    $missing = @()
    foreach ($location in $locations) {
        if (-not (Test-Path $location)) {
            $missing += $location
        }
    }
    $state.Missing = $missing
    return $state
}

Write-Host 'Personal-Agent start (single instance, one worker, no reload)' -ForegroundColor Cyan
Write-Host ('repository: {0}' -f $script:repoRoot)

Push-Location $script:repoRoot
try {
    if (-not (Test-Path (Join-Path $script:repoRoot 'code/backend/pyproject.toml'))) {
        Stop-WithError 'code/backend/pyproject.toml was not found.' @(
            'Run the script from the repository root or keep code/scripts inside the repository.',
            'Expected layout: code/backend, code/frontend, code/runtime-policy, code/scripts.'
        )
    }

    # ------------------------------------------------------------------ uv
    Write-Section 'uv toolchain'
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        Stop-WithError 'uv was not found on PATH.' @(
            'Install uv >=0.8,<1 (AGENTS.md section 2) and reopen the terminal.',
            'Pinned interpreter: CPython 3.12.9 via uv python install 3.12.9.'
        )
    }
    $uvExe = $uvCommand.Source
    if ([string]::IsNullOrWhiteSpace($uvExe)) {
        $uvExe = $uvCommand.Path
    }
    $global:LASTEXITCODE = 0
    $uvVersionText = ((& $uvExe --version) -join ' ').Trim()
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError 'uv --version failed.' @('Repair the uv installation before starting the application.')
    }
    $uvVersionMatch = [regex]::Match($uvVersionText, 'uv\s+(\d+\.\d+\.\d+)')
    if (-not $uvVersionMatch.Success) {
        Stop-WithError ('Could not parse the uv version from: ' + $uvVersionText) @('Print it with: uv --version')
    }
    $uvVersion = $uvVersionMatch.Groups[1].Value
    $uvVersionObject = [version]$uvVersion
    if ($uvVersionObject -lt [version]'0.8.0' -or $uvVersionObject -ge [version]'1.0.0') {
        Stop-WithError ('uv {0} is outside the required range >=0.8,<1.' -f $uvVersion) @(
            'Upgrade uv to a 0.13.x release (see AGENTS.md and docs/dependency-baseline.md).',
            'Do not continue with a floating or unknown uv version.'
        )
    }
    Write-Pass ('uv {0} at {1}' -f $uvVersion, $uvExe)

    # -------------------------------------------------------------- Python
    Write-Section 'pinned CPython'
    $global:LASTEXITCODE = 0
    $pythonPathOutput = & $uvExe python find 3.12
    $pythonPath = (($pythonPathOutput | Select-Object -First 1) -as [string])
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonPath)) {
        Stop-WithError 'uv could not find CPython 3.12.' @(
            'Install the pinned interpreter: uv python install 3.12.9',
            'Never run this project with the system Python 3.11.3.'
        )
    }
    $pythonPath = $pythonPath.Trim()
    $global:LASTEXITCODE = 0
    $pythonVersion = ((& $pythonPath -c "import platform; print(platform.python_version())") -join '').Trim()
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError ('The uv-managed interpreter could not be executed: ' + $pythonPath) @(
            'Reinstall it with: uv python install --reinstall 3.12.9'
        )
    }
    if ($pythonVersion -ne '3.12.9') {
        Stop-WithError ('CPython {0} was selected; this project is pinned to CPython 3.12.9.' -f $pythonVersion) @(
            'Install the exact interpreter: uv python install 3.12.9',
            'Then pin it for the project: uv python pin 3.12.9 (commit the pin if the repository uses one).'
        )
    }
    Write-Pass ('CPython {0} at {1}' -f $pythonVersion, $pythonPath)

    # ---------------------------------------------------- runtime configuration
    # Resolved first so a second instance or a bad port fails before any sync,
    # migration or self-check work touches the data directory.
    Write-Section 'runtime configuration'
    if ([string]::IsNullOrWhiteSpace($Mode)) {
        if (-not [string]::IsNullOrWhiteSpace($env:PERSONAL_AGENT_MODE)) {
            $Mode = $env:PERSONAL_AGENT_MODE
        }
        else {
            $Mode = 'mock'
        }
    }
    $resolvedMode = $Mode.Trim().ToLowerInvariant()
    if ($resolvedMode -ne 'mock' -and $resolvedMode -ne 'live') {
        Stop-WithError ("Unsupported -Mode '{0}'." -f $Mode) @(
            'Use -Mode mock (default, fully offline) or -Mode live (only after the runtime adoption gate).'
        )
    }

    if ([string]::IsNullOrWhiteSpace($BindHost)) {
        if (-not [string]::IsNullOrWhiteSpace($env:PERSONAL_AGENT_HOST)) {
            $BindHost = $env:PERSONAL_AGENT_HOST
        }
        else {
            $BindHost = '127.0.0.1'
        }
    }
    $resolvedHost = $BindHost.Trim()
    if ($resolvedHost -notmatch '^[A-Za-z0-9\.\-\:\[\]]+$') {
        Stop-WithError ("Invalid -Host '{0}'." -f $BindHost) @(
            'Use an IP address or host name, e.g. 127.0.0.1 or localhost.'
        )
    }

    if ($Port -eq 0) {
        $envPortValue = 0
        if (-not [string]::IsNullOrWhiteSpace($env:PERSONAL_AGENT_PORT)) {
            if (-not [int]::TryParse($env:PERSONAL_AGENT_PORT, [ref]$envPortValue)) {
                Stop-WithError ('PERSONAL_AGENT_PORT is not a number: ' + $env:PERSONAL_AGENT_PORT) @('Set it to a TCP port between 1 and 65535, or pass -Port.')
            }
            $Port = $envPortValue
        }
        else {
            $Port = 8000
        }
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        Stop-WithError ('Invalid -Port {0}.' -f $Port) @('Use a TCP port between 1 and 65535.')
    }

    if ([string]::IsNullOrWhiteSpace($DataDir)) {
        if (-not [string]::IsNullOrWhiteSpace($env:PERSONAL_AGENT_DATA_DIR)) {
            $DataDir = $env:PERSONAL_AGENT_DATA_DIR
        }
    }
    if ([string]::IsNullOrWhiteSpace($DataDir)) {
        if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            $DataDir = Join-Path $env:LOCALAPPDATA 'Personal-Agent'
        }
        else {
            $DataDir = Join-Path $HOME '.personal-agent'
        }
    }
    if ([System.IO.Path]::IsPathRooted($DataDir)) {
        $resolvedDataDir = [System.IO.Path]::GetFullPath($DataDir)
    }
    else {
        $resolvedDataDir = [System.IO.Path]::GetFullPath((Join-Path $script:repoRoot $DataDir))
    }
    try {
        New-Item -ItemType Directory -Force -Path $resolvedDataDir | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $resolvedDataDir 'db') | Out-Null
        $writeProbe = Join-Path $resolvedDataDir ('.write-probe-' + [guid]::NewGuid().ToString('N'))
        [System.IO.File]::WriteAllText($writeProbe, 'ok')
        Remove-Item $writeProbe -Force
    }
    catch {
        Stop-WithError ('The data directory is not writable: ' + $resolvedDataDir) @(
            'Choose another location with -DataDir (for example .cache\start-data).',
            'Fix NTFS permissions; the application never falls back to a different directory silently.'
        )
    }
    Write-Pass ('mode={0} host={1} port={2}' -f $resolvedMode, $resolvedHost, $Port)
    Write-Pass ('data directory: ' + $resolvedDataDir)

    if ($resolvedMode -eq 'live' -and $env:PERSONAL_AGENT_RUNTIME_ADOPTED -ne '1') {
        Stop-WithError 'Live mode is disabled: the runtime adoption gate has not passed.' @(
            'See docs/runtime-adoption.md (T021) and docs/live-acceptance.md (T100).',
            'Use -Mode mock for the verified offline path; Mock evidence must not be presented as live readiness.',
            'Never set PERSONAL_AGENT_RUNTIME_ADOPTED=1 unless the recorded gate evidence exists.'
        )
    }

    if (-not (Test-Path 'code/frontend/dist/index.html')) {
        Write-WarningLine 'code/frontend/dist/index.html is missing; serving the API only. Build it with: npm run build --prefix code/frontend (or code/scripts/package.ps1).'
        $frontendStatus = 'API only (no frontend build)'
    }
    else {
        $frontendStatus = 'same-origin UI + API (code/frontend/dist)'
        Write-Pass 'frontend build found: code/frontend/dist/index.html'
    }

    $lockPath = Join-Path (Join-Path $resolvedDataDir 'runtime') 'instance.lock'
    if (Test-InstanceLockHeld $lockPath) {
        # The owner writes its PID into the lock file, but that byte range is
        # locked while alive, so it is normally unreadable from here.
        Stop-WithError 'Another Personal-Agent instance already holds the single-instance lock for this data directory.' @(
            'Stop the existing instance first: a second worker is never started.',
            'Or use an isolated instance: -DataDir .cache\start-<name>.',
            ('Lock file: ' + $lockPath)
        ) -ExitCode 2
    }

    if (-not (Test-TcpPortAvailable -Address $resolvedHost -LocalPort $Port)) {
        Stop-WithError ('TCP port {0} on {1} is already in use.' -f $Port, $resolvedHost) @(
            'Stop the process using it or pass another -Port.',
            'Inspect it with: Get-NetTCPConnection -LocalPort <port> -State Listen'
        )
    }
    Write-Pass ('TCP port {0} is free' -f $Port)

    # ----------------------------------------------------------- dependency sync
    Write-Section 'locked Python dependencies'
    if ($NoSync) {
        Write-WarningLine 'uv sync skipped because -NoSync was passed; using the existing backend environment.'
    }
    else {
        # --frozen: uv.lock is never rewritten during a start; --inexact: an
        # already installed developer toolchain (ruff/mypy/pytest) is left in
        # place so code/scripts/check.ps1 keeps working. The runtime set itself
        # still comes exactly from the committed lock file.
        Invoke-Checked -Label 'uv lock --check' -FilePath $uvExe -Arguments @(
            'lock', '--check', '--project', 'code/backend'
        ) -Hints @(
            'uv.lock no longer matches code/backend/pyproject.toml.',
            'Regenerate and review it: uv lock --project code/backend (never hand-edit the lock).',
            'Record fresh lock fingerprints in docs/dependency-baseline.md.'
        )
        Invoke-Checked -Label 'uv sync --frozen' -FilePath $uvExe -Arguments @(
            'sync', '--frozen', '--inexact', '--project', 'code/backend', '--python', '3.12'
        ) -Hints @(
            'Run manually: uv sync --frozen --project code/backend --python 3.12',
            'Check network/proxy access to the package index and free disk space.',
            'Do not delete uv.lock: it is committed and defines the reproducible set.'
        )
    }
    $global:LASTEXITCODE = 0
    $envPythonArguments = Get-UvRunArguments @('python', '-c', 'import platform; print(platform.python_version())')
    $envPythonVersion = ((& $uvExe @envPythonArguments) -join '').Trim()
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError 'The backend virtual environment cannot execute Python.' @(
            'Recreate it: uv sync --frozen --project code/backend --python 3.12',
            'Then re-run this script.'
        )
    }
    if ($envPythonVersion -ne '3.12.9') {
        Stop-WithError ('The backend environment uses CPython {0}, expected 3.12.9.' -f $envPythonVersion) @(
            'Remove code/backend/.venv and re-run: uv sync --frozen --project code/backend --python 3.12'
        )
    }
    Write-Pass ('backend environment uses CPython {0}' -f $envPythonVersion)

    # ------------------------------------------------------- JS dependency installs
    Write-Section 'JavaScript dependency installs'
    $requiredLocks = @(
        'code/backend/uv.lock',
        'code/frontend/package-lock.json',
        'code/runtime-policy/package-lock.json'
    )
    $missingLocks = @()
    foreach ($lock in $requiredLocks) {
        if (-not (Test-Path $lock)) {
            $missingLocks += $lock
        }
    }
    if ($missingLocks.Count -gt 0) {
        Stop-WithError ('Required lock file(s) are missing: ' + ($missingLocks -join ', ')) @(
            'Restore them from version control; lockfiles are committed and must not be hand-edited.',
            'Regenerate only by re-locking dependencies, then update docs/dependency-baseline.md.'
        )
    }
    Write-Pass ('lock files present: ' + ($requiredLocks -join ', '))

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand -or $null -eq $npmCommand) {
        Write-WarningLine 'node/npm were not found on PATH; only the existing node_modules can be used.'
    }
    else {
        $global:LASTEXITCODE = 0
        $nodeVersion = (((& $nodeCommand.Source --version) -join '') -as [string]).Trim()
        if ($LASTEXITCODE -ne 0) {
            Write-WarningLine 'node --version failed; the Node.js toolchain is unverified.'
        }
        elseif ($nodeVersion -notmatch '^v24\.') {
            Write-WarningLine ('Node.js {0} is outside the required 24.x line; builds may not match the pinned toolchain.' -f $nodeVersion)
        }
        else {
            Write-Pass ('Node.js ' + $nodeVersion)
        }
        $global:LASTEXITCODE = 0
        $npmVersion = (((& $npmCommand.Source --version) -join '') -as [string]).Trim()
        if ($LASTEXITCODE -ne 0) {
            Write-WarningLine 'npm --version failed; the npm toolchain is unverified.'
        }
        elseif ($npmVersion -notmatch '^11\.') {
            Write-WarningLine ('npm {0} is outside the required 11.x line; npm ci may produce a different tree.' -f $npmVersion)
        }
        else {
            Write-Pass ('npm ' + $npmVersion)
        }
    }

    $missingModules = @()
    if (-not (Test-Path 'code/frontend/node_modules')) {
        $missingModules += 'code/frontend/node_modules'
    }
    if (-not (Test-Path 'code/runtime-policy/node_modules')) {
        $missingModules += 'code/runtime-policy/node_modules'
    }
    if ($missingModules.Count -gt 0) {
        Stop-WithError ('JavaScript dependencies are not installed: ' + ($missingModules -join ', ')) @(
            'Run: npm ci --prefix code/frontend',
            'Run: npm ci --prefix code/runtime-policy',
            'These installs must use the committed package-lock.json (no floating ranges).'
        )
    }
    Write-Pass 'code/frontend/node_modules and code/runtime-policy/node_modules are present'

    # --------------------------------------------------------- import self-check
    Write-Section 'backend import self-check'
    $importProbe = @(
        'import platform',
        'import importlib.metadata as metadata',
        'import personal_agent.api.app',
        'print(platform.python_version())',
        "print(metadata.version('playwright'))"
    ) -join '; '
    $importArguments = Get-UvRunArguments @('python', '-c', $importProbe)
    $global:LASTEXITCODE = 0
    $importOutput = & $uvExe @importArguments
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError 'The backend import smoke test failed.' @(
            'Run: uv run --no-sync --project code/backend python -c "import personal_agent.api.app"',
            'Check that uv sync completed and the working tree was not edited into an inconsistent state.'
        )
    }
    $importLines = @($importOutput | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne '' })
    if ($importLines.Count -lt 2) {
        Stop-WithError 'The import smoke test produced no version output.' @('Re-run it manually to see the Python error.')
    }
    if ($importLines[0] -ne '3.12.9') {
        Stop-WithError ('Import smoke ran on CPython {0}, expected 3.12.9.' -f $importLines[0]) @(
            'Recreate the environment: uv sync --frozen --project code/backend --python 3.12'
        )
    }
    $playwrightVersion = $importLines[1]
    Write-Pass ('backend imports ok (CPython {0}, Playwright {1})' -f $importLines[0], $playwrightVersion)

    # ------------------------------------------------------ pinned browser check
    Write-Section 'pinned browser (Playwright)'
    $browserState = Get-PlaywrightBrowserState -UvExe $uvExe
    $browserStatus = 'unverified'
    if ($null -eq $browserState.Revision) {
        Write-WarningLine 'Could not read the pinned Playwright browser revision (dry run failed); browser capabilities are unverified.'
    }
    else {
        Write-Note ('pinned Chromium revision: {0}' -f $browserState.Revision)
        if ($null -ne $browserState.ChromiumPath) {
            Write-Note ('expected install path: {0}' -f $browserState.ChromiumPath)
        }
        $baselinePath = Join-Path $script:repoRoot 'docs/dependency-baseline.md'
        if (Test-Path $baselinePath) {
            $baselineText = Get-Content $baselinePath -Raw
            $baselineRevision = [regex]::Match($baselineText, 'Chromium revision\s*\|\s*(\d+)')
            if ($baselineRevision.Success -and $baselineRevision.Groups[1].Value -ne $browserState.Revision) {
                Write-WarningLine ('docs/dependency-baseline.md records Chromium revision {0}, but the locked Playwright pins {1}; update the baseline.' -f $baselineRevision.Groups[1].Value, $browserState.Revision)
            }
        }
        $missingBrowserPaths = @($browserState.Missing)
        if ($missingBrowserPaths.Count -eq 0) {
            Write-Pass ('pinned Chromium revision {0} is installed' -f $browserState.Revision)
            $browserStatus = ('Chromium r{0} installed' -f $browserState.Revision)
        }
        elseif ($InstallBrowsers) {
            Write-Note ('installing the pinned Chromium revision (network download): ' + ($missingBrowserPaths -join ', '))
            $installArguments = Get-UvRunArguments @('python', '-m', 'playwright', 'install', 'chromium')
            Invoke-Checked -Label 'playwright install chromium' -FilePath $uvExe -Arguments $installArguments -Hints @(
                'This downloads the Chromium build pinned by the locked Playwright version.',
                'Requires network access; do not substitute an unpinned browser revision.'
            )
            $browserState = Get-PlaywrightBrowserState -UvExe $uvExe
            if (@($browserState.Missing).Count -gt 0) {
                Stop-WithError ('Playwright browser install did not complete; still missing: ' + (@($browserState.Missing) -join ', ')) @(
                    'Check network/proxy access and free disk space, then re-run with -InstallBrowsers.'
                )
            }
            Write-Pass ('pinned Chromium revision {0} is installed' -f $browserState.Revision)
            $browserStatus = ('Chromium r{0} installed' -f $browserState.Revision)
        }
        elseif ($resolvedMode -eq 'live') {
            Stop-WithError ('Live mode requires the pinned browser, but it is not installed: ' + ($missingBrowserPaths -join ', ')) @(
                'Re-run with -InstallBrowsers.',
                'Then re-run the runtime adoption checks before claiming live readiness.'
            )
        }
        else {
            Write-WarningLine 'Pinned Chromium is not installed; browser.read will be unavailable in this mock instance. Re-run with -InstallBrowsers or: uv run --project code/backend python -m playwright install chromium'
            $browserStatus = ('Chromium r{0} NOT installed (browser.read unavailable)' -f $browserState.Revision)
        }
    }

    # --------------------------------------------------------------- environment
    $dbPath = Join-Path (Join-Path $resolvedDataDir 'db') 'personal-agent.db'
    $dbUrl = 'sqlite+aiosqlite:///' + ($dbPath -replace '\\', '/')
    # bootstrap/run.py and config.py resolve these variables; the CLI flags below
    # repeat them explicitly so the effective values are visible in the trace.
    $env:PERSONAL_AGENT_MODE = $resolvedMode
    $env:PERSONAL_AGENT_HOST = $resolvedHost
    $env:PERSONAL_AGENT_PORT = [string]$Port
    $env:PERSONAL_AGENT_DATA_DIR = $resolvedDataDir
    $env:PERSONAL_AGENT_DB_URL = $dbUrl

    # --------------------------------------------------------------- migrations
    Write-Section 'database migrations'
    $migrationArguments = Get-UvRunArguments @('alembic', '-c', 'code/backend/alembic.ini', 'upgrade', 'head')
    Invoke-Checked -Label 'alembic upgrade head' -FilePath $uvExe -Arguments $migrationArguments -Hints @(
        ('Database: ' + $dbPath),
        'Confirm the data directory is writable and no other instance is migrating.',
        'Never point PERSONAL_AGENT_DB_URL at the day-to-day database during tests.'
    )
    Write-Pass 'database is at the migration head'

    # ------------------------------------------------------------------- seed
    Write-Section 'seed workflows (idempotent)'
    $seedArguments = Get-UvRunArguments @('python', '-m', 'personal_agent.bootstrap.seed', '--data-dir', $resolvedDataDir)
    Invoke-Checked -Label 'seed workflows' -FilePath $uvExe -Arguments $seedArguments -Hints @(
        'Seeding goes through registry validation; a second run must create nothing.',
        'Run migrations first if this step reports a schema mismatch.'
    )
    Write-Pass 'seed workflows are present'

    # ------------------------------------------------------- offline diagnostics
    Write-Section 'offline startup diagnostics'
    $diagnosticArguments = Get-UvRunArguments @(
        'python', '-m', 'personal_agent.bootstrap.run',
        '--mode', $resolvedMode,
        '--host', $resolvedHost,
        '--port', [string]$Port,
        '--data-dir', $resolvedDataDir,
        '--check'
    )
    $global:LASTEXITCODE = 0
    $diagnosticOutput = & $uvExe @diagnosticArguments
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError ('Offline startup diagnostics failed (exit {0}).' -f $LASTEXITCODE) @(
            'The JSON report above names the failing dependency check.',
            'Re-run migrations, then retry this script.',
            'For live mode, re-verify the runtime adoption gate.'
        )
    }
    $diagnosticText = ((@($diagnosticOutput) | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    $diagnostic = $null
    try {
        $diagnostic = $diagnosticText | ConvertFrom-Json
    }
    catch {
        Stop-WithError 'Could not parse the offline diagnostics JSON.' @('Re-run the command manually to inspect the raw output.')
    }
    if (-not $diagnostic.migrations_up_to_date) {
        Stop-WithError ('Database migrations are not at head (current {0}, head {1}).' -f $diagnostic.migration_current, $diagnostic.migration_head) @(
            'Run: uv run --project code/backend alembic -c code/backend/alembic.ini upgrade head'
        )
    }
    if (-not $diagnostic.execution_available) {
        Stop-WithError ("Execution is not available in mode '{0}'." -f $resolvedMode) @(
            'Mock mode must report execution_available=true.',
            'Live mode requires the recorded runtime adoption gate and its evidence.'
        )
    }
    Write-Pass ('diagnostics ok: migrations={0}, execution_available={1}, runtime={2}' -f $diagnostic.migrations_up_to_date, $diagnostic.execution_available, $diagnostic.runtime.adapter_version)

    # ------------------------------------------------------------------ summary
    Write-Section 'startup plan'
    Write-Host ('  URL            http://{0}:{1}/' -f $resolvedHost, $Port)
    Write-Host ('  mode           {0}' -f $resolvedMode)
    Write-Host ('  data dir       {0}' -f $resolvedDataDir)
    Write-Host ('  database       {0}' -f $dbPath)
    Write-Host ('  frontend       {0}' -f $frontendStatus)
    Write-Host ('  browser        {0}' -f $browserStatus)
    Write-Host ('  instance lock  {0}' -f $lockPath)
    Write-Host '  worker         1 (uvicorn default; reload disabled)'

    if ($CheckOnly) {
        Write-Host ''
        Write-Host 'Self-check passed; -CheckOnly set, so nothing was served.' -ForegroundColor Green
        exit 0
    }

    Write-Host ''
    Write-Host ('Starting Personal-Agent at http://{0}:{1}/  (Ctrl+C to stop)' -f $resolvedHost, $Port) -ForegroundColor Green
    $serveArguments = Get-UvRunArguments @(
        'python', '-m', 'personal_agent.bootstrap.run',
        '--mode', $resolvedMode,
        '--host', $resolvedHost,
        '--port', [string]$Port,
        '--data-dir', $resolvedDataDir
    )
    $global:LASTEXITCODE = 0
    & $uvExe @serveArguments
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -eq 0) {
        Write-Host 'Personal-Agent stopped.'
        exit 0
    }
    if ($serverExitCode -eq 2) {
        Stop-WithError 'Another Personal-Agent instance is already running for this data directory; the OS lock refused this process.' @(
            'Stop the existing instance or pass a different -DataDir.',
            'No reload and no second worker were started.'
        ) -ExitCode 2
    }
    Stop-WithError ('The server exited with code {0}.' -f $serverExitCode) @(
        'Read the traceback above for the concrete cause.',
        'Re-run with -CheckOnly to isolate preflight problems from the serving process.'
    )
}
catch {
    if (-not $_.Exception.Data.Contains('StartupReported')) {
        Write-Host ''
        Write-Host ('ERROR: unexpected start failure: ' + $_.Exception.Message) -ForegroundColor Red
    }
    exit $script:failExitCode
}
finally {
    Pop-Location
}
