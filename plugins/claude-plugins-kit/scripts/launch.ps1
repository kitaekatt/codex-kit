$ErrorActionPreference = 'Stop'
$bridgeArguments = $args
function Test-BridgePython([string] $Executable) {
    try {
        & $Executable -I -c 'import sys; assert sys.version_info >= (3, 10)' *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}
try {
    $bridgePython = $env:CODEX_KIT_PYTHON
    if ($bridgePython) {
        if (-not (Test-BridgePython $bridgePython)) { throw 'CODEX_KIT_PYTHON must be executable Python >=3.10.' }
    } else {
        foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\share\python-standalone\python\python.exe'), 'python3', 'python')) {
            if (Test-BridgePython $candidate) { $bridgePython = $candidate; break }
        }
        if (-not $bridgePython) { $bridgePython = & (Join-Path $PSScriptRoot 'runtime.ps1') }
    }
    & $bridgePython -I (Join-Path $PSScriptRoot 'bridge.py') @bridgeArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("Claude Plugins Kit runtime: $_")
    exit 2
}
