$ErrorActionPreference = 'Stop'
function Assert-SafePath([string] $Path) {
    $probe = [IO.Path]::GetFullPath($Path)
    while ($probe) {
        if (Test-Path -LiteralPath $probe) {
            if ((Get-Item -LiteralPath $probe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Runtime path traverses a reparse point: $probe"
            }
        }
        if (Test-Path -LiteralPath (Join-Path $probe '.git')) { throw "Runtime path is in a Git worktree: $probe" }
        $parent = [IO.Directory]::GetParent($probe)
        $probe = if ($parent) { $parent.FullName } else { $null }
    }
}
$runtimeArch = $env:PROCESSOR_ARCHITEW6432
if (-not $runtimeArch) { $runtimeArch = $env:PROCESSOR_ARCHITECTURE }
$asset = @(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-assets.tsv') | ForEach-Object {
    $fields = $_ -split '\s+'
    if ($fields[0] -eq 'Windows' -and $fields[1] -eq $runtimeArch) { ,$fields }
})
if ($asset.Count -ne 1) { throw "Unsupported Windows architecture: $runtimeArch; set CODEX_KIT_PYTHON." }
$runtimeTarget = $asset[0][2]
$runtimeSha = $asset[0][3]
$runtimeData = $env:CODEX_KIT_DATA_ROOT
if (-not $runtimeData) { $runtimeData = Join-Path $env:LOCALAPPDATA 'codex-kit' }
if (-not [IO.Path]::IsPathRooted($runtimeData)) { throw 'Runtime data root must be absolute.' }
Assert-SafePath $runtimeData
$runtimeBase = Join-Path $runtimeData 'runtime'
$runtimeRoot = Join-Path $runtimeBase "cpython-3.13.15-20260901-$runtimeTarget"
Assert-SafePath $runtimeRoot
$runtimePython = Join-Path $runtimeRoot 'python\python.exe'
$marker = Join-Path $runtimeRoot '.codex-kit-runtime'
function Assert-Runtime {
    Assert-SafePath $marker
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne $runtimeSha) {
        throw "Unowned or damaged private runtime: $runtimeRoot"
    }
    & $runtimePython -I -c 'import sys; assert sys.version_info >= (3, 10)' *> $null
    if ($LASTEXITCODE -ne 0) { throw "Private runtime cannot run: $runtimeRoot" }
}
if (-not (Test-Path -LiteralPath $runtimeRoot)) {
    if ($env:CODEX_KIT_RUNTIME_DOWNLOAD -eq '0') { throw 'Private runtime download disabled; set CODEX_KIT_PYTHON.' }
    if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { throw 'Windows tar.exe is required to extract the private runtime.' }
    [IO.Directory]::CreateDirectory($runtimeBase) | Out-Null
    $lockPath = Join-Path $runtimeBase '.install.lock'
    Assert-SafePath $lockPath
    $lockStream = $null
    $stage = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    try {
        while (-not $lockStream) {
            try { $lockStream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
            catch [IO.IOException] {
                if ([DateTime]::UtcNow -ge $deadline) { throw 'Another private runtime installation is still running.' }
                Start-Sleep -Milliseconds 200
            }
        }
        if (-not (Test-Path -LiteralPath $runtimeRoot)) {
            $stage = Join-Path $runtimeBase ('.install.' + [Guid]::NewGuid().ToString('N'))
            [IO.Directory]::CreateDirectory($stage) | Out-Null
            $archive = Join-Path $stage 'python.tar.gz'
            $url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-$runtimeTarget-install_only_stripped.tar.gz"
            [Console]::Error.WriteLine('Claude Plugins Kit: downloading private Python 3.13.15 from python-build-standalone.')
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 180
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeSha) {
                throw 'Download SHA256 mismatch; archive was not executed.'
            }
            $payload = Join-Path $stage 'payload'
            [IO.Directory]::CreateDirectory($payload) | Out-Null
            & tar.exe -xzf $archive -C $payload
            if ($LASTEXITCODE -ne 0) { throw 'Private runtime extraction failed.' }
            & (Join-Path $payload 'python\python.exe') -I -c 'import sys; assert sys.version_info >= (3, 10)' *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Downloaded interpreter cannot run on this platform.' }
            [IO.File]::WriteAllText((Join-Path $payload '.codex-kit-runtime'), $runtimeSha)
            [IO.Directory]::Move($payload, $runtimeRoot)
        }
    } finally {
        if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
        if ($lockStream) { $lockStream.Dispose() }
    }
}
Assert-Runtime
$runtimePython
