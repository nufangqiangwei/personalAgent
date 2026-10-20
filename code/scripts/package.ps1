# Personal-Agent reproducible package builder (T101).
#
# Runs the pinned type checks and unit tests, builds the frontend, the runtime
# policy plugin and the backend wheel/sdist, then collects everything under one
# versioned output directory with a deterministic manifest (no timestamps) that
# records file hashes, toolchain versions, browser revision and lockfile
# fingerprints. SOURCE_DATE_EPOCH is fixed before building so the backend
# distributions are byte-identical across repeated builds.
#
# Usage (repository root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/package.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/package.ps1 -Output dist -SkipTests
#
# Default output: dist/personal-agent-<version>/ (outside specs/, git-ignored).
# Exit code 0 = package produced, 1 = a check/build failed.

[CmdletBinding()]
param(
    [string]$Output = 'dist',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$script:repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $script:repoRoot 'AGENTS.md'))) {
    $script:repoRoot = (Get-Location).Path
}
$script:failExitCode = 1
$script:stageFull = $null
$script:stageVerified = $false
$script:previousSourceDateEpoch = $null
$script:epochApplied = $false

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
    $exception.Data['PackageReported'] = $true
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
    return @('run', '--no-sync', '--project', 'code/backend', '--python', '3.12') + $CommandArguments
}

function Get-ToolVersion {
    param([string]$FilePath, [string]$VersionArgument = '--version')
    $global:LASTEXITCODE = 0
    $text = (((& $FilePath $VersionArgument) -join '') -as [string]).Trim()
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return $text
}

function Get-PinnedBrowser {
    param([string]$UvExe)
    $result = New-Object PSObject -Property @{
        Revision = $null
        Installed = $false
    }
    $dryRunArguments = Get-UvRunArguments @('python', '-m', 'playwright', 'install', '--dry-run', 'chromium')
    $global:LASTEXITCODE = 0
    $output = & $UvExe @dryRunArguments
    if ($LASTEXITCODE -ne 0) {
        return $result
    }
    $revision = $null
    $chromiumPath = $null
    foreach ($item in $output) {
        $line = [string]$item
        if ($line -match 'playwright chromium v(\d+)') {
            $revision = $Matches[1]
        }
        elseif ($line -match '^\s*Install location:\s*(.+?)\s*$') {
            if ($null -eq $chromiumPath -and $Matches[1] -match 'chromium-\d+$') {
                $chromiumPath = $Matches[1]
            }
        }
    }
    $result.Revision = $revision
    if ($null -ne $chromiumPath) {
        $result.Installed = Test-Path $chromiumPath
    }
    return $result
}

function Get-GitValue {
    param([string[]]$Arguments)
    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        return $null
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $text = ((& git @Arguments) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return $text
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

Write-Host 'Personal-Agent package (reproducible artifacts)' -ForegroundColor Cyan
Write-Host ('repository: {0}' -f $script:repoRoot)

Push-Location $script:repoRoot
try {
    foreach ($required in @(
        'code/backend/pyproject.toml',
        'code/frontend/package.json',
        'code/runtime-policy/package.json'
    )) {
        if (-not (Test-Path $required)) {
            Stop-WithError ('Required project file is missing: ' + $required) @(
                'Run the script from the repository root.',
                'Expected layout: code/backend, code/frontend, code/runtime-policy, code/scripts.'
            )
        }
    }

    # ----------------------------------------------------------- toolchain
    Write-Section 'toolchain'
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        Stop-WithError 'uv was not found on PATH.' @('Install uv >=0.8,<1 (AGENTS.md section 2).')
    }
    $uvExe = $uvCommand.Source
    if ([string]::IsNullOrWhiteSpace($uvExe)) {
        $uvExe = $uvCommand.Path
    }
    $uvVersionText = Get-ToolVersion -FilePath $uvExe
    $uvVersionMatch = [regex]::Match([string]$uvVersionText, 'uv\s+(\d+\.\d+\.\d+)')
    if (-not $uvVersionMatch.Success) {
        Stop-WithError ('Could not parse the uv version from: ' + $uvVersionText) @('Print it with: uv --version')
    }
    $uvVersion = $uvVersionMatch.Groups[1].Value
    $uvVersionObject = [version]$uvVersion
    if ($uvVersionObject -lt [version]'0.8.0' -or $uvVersionObject -ge [version]'1.0.0') {
        Stop-WithError ('uv {0} is outside the required range >=0.8,<1.' -f $uvVersion) @('Upgrade to a pinned 0.13.x release.')
    }
    Write-Pass ('uv ' + $uvVersion)

    $global:LASTEXITCODE = 0
    $pythonPath = ((& $uvExe python find 3.12 | Select-Object -First 1) -as [string])
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonPath)) {
        Stop-WithError 'uv could not find CPython 3.12.' @('Install it with: uv python install 3.12.9')
    }
    $pythonPath = $pythonPath.Trim()
    $pythonVersion = ((& $pythonPath -c "import platform; print(platform.python_version())") -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.9') {
        Stop-WithError ('CPython {0} was selected; this project is pinned to CPython 3.12.9.' -f $pythonVersion) @(
            'Install the exact interpreter: uv python install 3.12.9'
        )
    }
    Write-Pass ('CPython ' + $pythonVersion)

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand -or $null -eq $npmCommand) {
        Stop-WithError 'node/npm were not found on PATH.' @('Install Node.js 24.x and npm 11.x (AGENTS.md section 3).')
    }
    $nodeVersion = Get-ToolVersion -FilePath $nodeCommand.Source
    if ($nodeVersion -notmatch '^v24\.') {
        Stop-WithError ('Node.js {0} is outside the required 24.x line.' -f $nodeVersion) @('Install Node.js 24.x; builds must use the pinned toolchain.')
    }
    $npmVersion = Get-ToolVersion -FilePath $npmCommand.Source
    if ($npmVersion -notmatch '^11\.') {
        Stop-WithError ('npm {0} is outside the required 11.x line.' -f $npmVersion) @('Install npm 11.x; npm ci must use the pinned toolchain.')
    }
    Write-Pass ('Node.js ' + $nodeVersion + ', npm ' + $npmVersion)
    $npmExe = $npmCommand.Source

    # ------------------------------------------------------------- versions
    Write-Section 'product versions'
    $backendPyproject = Get-Content 'code/backend/pyproject.toml' -Raw
    $backendVersionMatch = [regex]::Match($backendPyproject, '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $backendVersionMatch.Success) {
        Stop-WithError 'Could not read [project].version from code/backend/pyproject.toml.' @('Keep a single fixed semver version in pyproject.toml.')
    }
    $backendVersion = $backendVersionMatch.Groups[1].Value
    $frontendVersion = [string]((Get-Content 'code/frontend/package.json' -Raw | ConvertFrom-Json).version)
    $policyVersion = [string]((Get-Content 'code/runtime-policy/package.json' -Raw | ConvertFrom-Json).version)
    if ($backendVersion -ne $frontendVersion -or $backendVersion -ne $policyVersion) {
        Stop-WithError ('Product versions differ: backend {0}, frontend {1}, runtime-policy {2}.' -f $backendVersion, $frontendVersion, $policyVersion) @(
            'Align the versions before packaging; one artifact must describe one product version.'
        )
    }
    if ($backendVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+([\-\.][0-9A-Za-z\.\-]+)?$') {
        Stop-WithError ('Unsupported version format: ' + $backendVersion) @('Use a semver-like version such as 0.1.0.')
    }
    Write-Pass ('product version ' + $backendVersion + ' (backend/frontend/runtime-policy aligned)')

    # --------------------------------------------------------- output layout
    Write-Section 'output layout'
    if ([string]::IsNullOrWhiteSpace($Output)) {
        Stop-WithError '-Output must not be empty.' @('Use the default -Output dist or another directory outside specs/.')
    }
    if ([System.IO.Path]::IsPathRooted($Output)) {
        $outputFull = [System.IO.Path]::GetFullPath($Output)
    }
    else {
        $outputFull = [System.IO.Path]::GetFullPath((Join-Path $script:repoRoot $Output))
    }
    $outputFull = $outputFull.TrimEnd('\', '/')
    if ($outputFull -match '[\\/]specs([\\/]|$)') {
        Stop-WithError 'Refusing to write build artifacts inside specs/.' @(
            'specs/ holds requirements and design only; choose -Output dist or another path outside specs/.'
        )
    }
    $script:stageFull = Join-Path $outputFull ('personal-agent-' + $backendVersion)
    $stageLeaf = Split-Path -Leaf $script:stageFull
    if ($stageLeaf -notmatch '^personal-agent-[0-9A-Za-z\.\-]+$') {
        Stop-WithError ('Refusing an unexpected stage directory name: ' + $stageLeaf) @('The output directory must be a plain filesystem path.')
    }
    $expectedPrefix = $outputFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $script:stageFull.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-WithError ('Refusing a stage directory outside -Output: ' + $script:stageFull) @('Pass a plain directory path in -Output.')
    }
    $script:stageVerified = $true
    if (Test-Path $script:stageFull) {
        Write-Note ('replacing previous package: ' + $script:stageFull)
        Remove-Item -Recurse -Force $script:stageFull
    }
    New-Item -ItemType Directory -Force -Path $script:stageFull | Out-Null
    Write-Pass ('package directory: ' + $script:stageFull)

    # ------------------------------------------------------------ dependencies
    Write-Section 'locked Python dependencies'
    Invoke-Checked -Label 'uv lock --check' -FilePath $uvExe -Arguments @(
        'lock', '--check', '--project', 'code/backend'
    ) -Hints @(
        'uv.lock no longer matches code/backend/pyproject.toml.',
        'Regenerate and review it: uv lock --project code/backend (never hand-edit the lock).'
    )
    Invoke-Checked -Label 'uv sync --frozen (dev extras)' -FilePath $uvExe -Arguments @(
        'sync', '--frozen', '--inexact', '--extra', 'dev', '--project', 'code/backend', '--python', '3.12'
    ) -Hints @(
        'Run manually: uv sync --frozen --extra dev --project code/backend --python 3.12',
        'Check network/proxy access to the package index and free disk space.'
    )
    Write-Pass 'backend environment synced from the committed lock'

    # ---------------------------------------------------------------- checks
    Write-Section 'type checks'
    $mypyArguments = Get-UvRunArguments @('mypy', 'code/backend/src')
    Invoke-Checked -Label 'backend mypy' -FilePath $uvExe -Arguments $mypyArguments -Hints @(
        'Fix the reported typing errors; packaging must not skip them.'
    )
    Invoke-Checked -Label 'frontend typecheck (vue-tsc)' -FilePath $npmExe -Arguments @(
        'run', 'typecheck', '--prefix', 'code/frontend'
    ) -Hints @('Fix the Vue/TypeScript errors before packaging.')
    Invoke-Checked -Label 'runtime-policy typecheck (tsc)' -FilePath $npmExe -Arguments @(
        'run', 'typecheck', '--prefix', 'code/runtime-policy'
    ) -Hints @('Fix the TypeScript errors before packaging.')
    Write-Pass 'type checks passed'

    if ($SkipTests) {
        Write-Section 'unit tests'
        Write-WarningLine 'unit tests skipped because -SkipTests was passed; the package is not test-evidenced.'
    }
    else {
        Write-Section 'unit tests (offline)'
        $backendTestArguments = Get-UvRunArguments @('python', '-m', 'pytest', 'code/backend/tests/unit', '-q')
        Invoke-Checked -Label 'backend unit tests' -FilePath $uvExe -Arguments $backendTestArguments -Hints @(
            'Fix the failures; do not package a red unit suite.'
        )
        Invoke-Checked -Label 'frontend unit tests' -FilePath $npmExe -Arguments @(
            'run', 'test', '--prefix', 'code/frontend', '--', '--run'
        ) -Hints @('Fix the failing Vitest specs before packaging.')
        Invoke-Checked -Label 'runtime-policy unit tests' -FilePath $npmExe -Arguments @(
            'run', 'test', '--prefix', 'code/runtime-policy', '--', '--run'
        ) -Hints @('Fix the failing policy tests before packaging.')
        Write-Pass 'offline unit tests passed'
    }

    # ---------------------------------------------------------------- builds
    Write-Section 'build artifacts'
    # Fixed epoch: hatchling stamps archive members from it, so the wheel and
    # sdist are byte-identical across repeated builds of the same commit.
    $script:previousSourceDateEpoch = $env:SOURCE_DATE_EPOCH
    $env:SOURCE_DATE_EPOCH = '946684800'
    $script:epochApplied = $true
    Write-Note 'SOURCE_DATE_EPOCH=946684800 (2000-01-01T00:00:00Z) for reproducible archives'

    $frontendDist = 'code/frontend/dist'
    if (Test-Path $frontendDist) {
        Remove-Item -Recurse -Force $frontendDist
    }
    Invoke-Checked -Label 'frontend build' -FilePath $npmExe -Arguments @(
        'run', 'build', '--prefix', 'code/frontend'
    ) -Hints @(
        'Fix the build errors and re-run; the same-origin UI is a package deliverable.',
        'The build command runs vue-tsc before vite build.'
    )
    if (-not (Test-Path (Join-Path $frontendDist 'index.html'))) {
        Stop-WithError 'The frontend build produced no index.html.' @('Inspect the vite build output above.')
    }
    Write-Pass 'frontend build complete'

    Invoke-Checked -Label 'runtime-policy build' -FilePath $npmExe -Arguments @(
        'run', 'build', '--prefix', 'code/runtime-policy'
    ) -Hints @('The policy plugin build is required for the production artifact set.')
    Write-Pass 'runtime-policy build complete'

    $backendStage = Join-Path $script:stageFull 'backend'
    New-Item -ItemType Directory -Force -Path $backendStage | Out-Null
    Invoke-Checked -Label 'uv build (backend)' -FilePath $uvExe -Arguments @(
        'build', '--project', 'code/backend', '--out-dir', $backendStage
    ) -Hints @(
        'Fix the packaging errors in code/backend/pyproject.toml / hatchling configuration.',
        'uv build must produce both the wheel and the sdist before packaging can succeed.'
    )
    $backendGitignore = Join-Path $backendStage '.gitignore'
    if (Test-Path $backendGitignore) {
        Remove-Item $backendGitignore -Force
    }
    $backendDistributions = @(Get-ChildItem -Path $backendStage -File | Where-Object {
        $_.Extension -eq '.whl' -or $_.Name -like '*.tar.gz'
    })
    if ($backendDistributions.Count -eq 0) {
        Stop-WithError 'uv build produced no wheel/sdist.' @('Inspect the uv build output above.')
    }
    Write-Pass ('backend distributions: ' + (($backendDistributions | ForEach-Object { $_.Name }) -join ', '))

    $frontendStage = Join-Path $script:stageFull 'frontend'
    New-Item -ItemType Directory -Force -Path $frontendStage | Out-Null
    Copy-Item -Path (Join-Path $frontendDist '*') -Destination $frontendStage -Recurse -Force
    if (-not (Test-Path (Join-Path $frontendStage 'index.html'))) {
        Stop-WithError 'Copying code/frontend/dist failed.' @('Check free disk space and NTFS permissions.')
    }
    Write-Pass 'frontend dist collected'

    $policyDist = 'code/runtime-policy/dist'
    if (-not (Test-Path (Join-Path $policyDist 'index.js'))) {
        Stop-WithError 'The runtime-policy build produced no dist/index.js.' @('Inspect the tsc output above.')
    }
    $policyStage = Join-Path (Join-Path $script:stageFull 'runtime-policy') 'dist'
    New-Item -ItemType Directory -Force -Path $policyStage | Out-Null
    Copy-Item -Path (Join-Path $policyDist '*') -Destination $policyStage -Recurse -Force
    Write-Pass 'runtime-policy dist collected'

    # -------------------------------------------------------------- manifest
    Write-Section 'manifest'
    $pythonProbe = @(
        'import platform',
        'import importlib.metadata as metadata',
        'print(platform.python_version())',
        "print(metadata.version('playwright'))"
    ) -join '; '
    $probeArguments = Get-UvRunArguments @('python', '-c', $pythonProbe)
    $global:LASTEXITCODE = 0
    $probeOutput = & $uvExe @probeArguments
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError 'Could not read the backend runtime versions.' @('Run the python version probe manually to see the error.')
    }
    $probeLines = @($probeOutput | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne '' })
    if ($probeLines.Count -lt 2) {
        Stop-WithError 'The version probe returned incomplete output.' @('Re-run: uv run --project code/backend python -c "import platform; print(platform.python_version())"')
    }
    $playwrightVersion = $probeLines[1]
    if ($probeLines[0] -ne '3.12.9') {
        Stop-WithError ('The package environment uses CPython {0}, expected 3.12.9.' -f $probeLines[0]) @(
            'Recreate it: uv sync --frozen --extra dev --project code/backend --python 3.12'
        )
    }

    $browser = Get-PinnedBrowser -UvExe $uvExe
    if ($null -eq $browser.Revision) {
        Write-WarningLine 'The pinned Playwright browser revision could not be read; the manifest records null.'
    }
    elseif (-not $browser.Installed) {
        Write-WarningLine ('Pinned Chromium revision {0} is not installed; the package still records the required revision.' -f $browser.Revision)
    }

    $gitCommit = Get-GitValue -Arguments @('rev-parse', 'HEAD')
    $gitStatus = Get-GitValue -Arguments @('status', '--porcelain')
    $gitDirty = $null
    if ($null -ne $gitStatus) {
        $gitDirty = $gitStatus.Length -gt 0
    }

    $lockRelativePaths = @(
        'code/backend/uv.lock',
        'code/frontend/package-lock.json',
        'code/runtime-policy/package-lock.json'
    )
    $lockEntries = [ordered]@{}
    foreach ($relativePath in $lockRelativePaths) {
        if (-not (Test-Path $relativePath)) {
            Stop-WithError ('Required lock file is missing: ' + $relativePath) @('Restore it from version control; lockfiles are committed.')
        }
        $lockEntries[$relativePath] = [ordered]@{
            sha256 = (Get-FileHash $relativePath -Algorithm SHA256).Hash.ToLowerInvariant()
            bytes = (Get-Item $relativePath).Length
        }
    }
    $baselinePath = Join-Path $script:repoRoot 'docs/dependency-baseline.md'
    if (Test-Path $baselinePath) {
        $baselineText = Get-Content $baselinePath -Raw
        foreach ($relativePath in $lockRelativePaths) {
            $actualHash = [string]$lockEntries[$relativePath].sha256
            $baselineMatch = [regex]::Match($baselineText, [regex]::Escape($relativePath) + '[^\r\n]*?([0-9a-f]{64})')
            if ($baselineMatch.Success -and $baselineMatch.Groups[1].Value -ne $actualHash) {
                Write-WarningLine ('docs/dependency-baseline.md records {0} for {1}, but the lock hashes to {2}; update the baseline.' -f $baselineMatch.Groups[1].Value, $relativePath, $actualHash)
            }
        }
    }

    $unitTestStatus = 'passed'
    if ($SkipTests) {
        $unitTestStatus = 'skipped'
    }

    $manifestPath = Join-Path $script:stageFull 'manifest.json'
    $sumsPath = Join-Path $script:stageFull 'SHA256SUMS.txt'
    $packageFiles = @(
        Get-ChildItem -Path $script:stageFull -Recurse -File |
            Where-Object { $_.FullName -ne $manifestPath -and $_.FullName -ne $sumsPath } |
            Sort-Object { $_.FullName }
    )
    $fileEntries = @()
    foreach ($packageFile in $packageFiles) {
        $relativePath = $packageFile.FullName.Substring($script:stageFull.Length + 1).Replace('\', '/')
        $fileEntries += [ordered]@{
            path = $relativePath
            sha256 = (Get-FileHash $packageFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            bytes = $packageFile.Length
        }
    }
    $totalBytes = 0
    foreach ($entry in $fileEntries) {
        $totalBytes += [long]$entry.bytes
    }

    $manifest = [ordered]@{
        schema = 1
        product = 'personal-agent'
        version = $backendVersion
        source = [ordered]@{
            git_commit = $gitCommit
            git_dirty = $gitDirty
        }
        toolchain = [ordered]@{
            uv = $uvVersion
            python = $probeLines[0]
            node = $nodeVersion
            npm = $npmVersion
        }
        versions = [ordered]@{
            backend = $backendVersion
            frontend = $frontendVersion
            runtime_policy = $policyVersion
        }
        checks = [ordered]@{
            type_checks = 'passed'
            unit_tests = $unitTestStatus
        }
        browsers = [ordered]@{
            playwright = $playwrightVersion
            chromium_revision = $browser.Revision
            chromium_installed = $browser.Installed
        }
        lockfiles = $lockEntries
        reproducible = [ordered]@{
            source_date_epoch = 946684800
            note = 'fixed SOURCE_DATE_EPOCH and no wall-clock fields; rebuild into the same -Output to compare hashes'
        }
        files = $fileEntries
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
    Write-Pass ('manifest.json written (' + $fileEntries.Count + ' artifact files, ' + $totalBytes + ' bytes)')

    $sumLines = @()
    foreach ($entry in $fileEntries) {
        $sumLines += ('{0}  {1}' -f $entry.sha256, $entry.path)
    }
    $manifestHash = (Get-FileHash $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $sumLines += ('{0}  {1}' -f $manifestHash, 'manifest.json')
    [System.IO.File]::WriteAllText($sumsPath, (($sumLines -join "`n") + "`n"), $utf8NoBom)
    Write-Pass ('SHA256SUMS.txt written (manifest sha256=' + $manifestHash + ')')

    # --------------------------------------------------------------- summary
    Write-Section 'package complete'
    Write-Host ('  output       {0}' -f $script:stageFull)
    Write-Host ('  manifest     {0}' -f $manifestPath)
    Write-Host ('  checksums    {0}' -f $sumsPath)
    Write-Host ('  version      {0}' -f $backendVersion)
    Write-Host ('  files        {0} ({1} bytes)' -f $fileEntries.Count, $totalBytes)
    Write-Host ('  wheel/sdist  {0}' -f (($backendDistributions | ForEach-Object { $_.Name }) -join ', '))
    foreach ($relativePath in $lockRelativePaths) {
        Write-Host ('  lock         {0}  {1}' -f $lockEntries[$relativePath].sha256, $relativePath)
    }
    if ($null -eq $browser.Revision) {
        Write-Host '  browser      pinned revision unverified'
    }
    else {
        Write-Host ('  browser      Chromium r{0} (installed={1})' -f $browser.Revision, $browser.Installed)
    }
    if ($SkipTests) {
        Write-WarningLine 'Built with -SkipTests: unit-test evidence is missing for this package.'
    }
    exit 0
}
catch {
    if (-not $_.Exception.Data.Contains('PackageReported')) {
        Write-Host ''
        Write-Host ('ERROR: unexpected package failure: ' + $_.Exception.Message) -ForegroundColor Red
    }
    if ($script:stageVerified -and $null -ne $script:stageFull -and (Test-Path $script:stageFull)) {
        try {
            Remove-Item -Recurse -Force $script:stageFull
            Write-Host ('Removed the incomplete package directory: ' + $script:stageFull) -ForegroundColor Yellow
        }
        catch {
            Write-Host ('Could not remove the incomplete package directory: ' + $script:stageFull) -ForegroundColor Yellow
        }
    }
    exit $script:failExitCode
}
finally {
    if ($script:epochApplied) {
        if ([string]::IsNullOrEmpty($script:previousSourceDateEpoch)) {
            Remove-Item Env:\SOURCE_DATE_EPOCH -ErrorAction SilentlyContinue
        }
        else {
            $env:SOURCE_DATE_EPOCH = $script:previousSourceDateEpoch
        }
    }
    Pop-Location
}
