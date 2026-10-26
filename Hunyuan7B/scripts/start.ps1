param([int]$Port = 8008, [switch]$Background)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pythonPath = Join-Path $repoRoot 'small_model/environments/hunyuan/Scripts/python.exe'
$serverPath = Join-Path $PSScriptRoot 'serve.py'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use"
}
if ($Background) {
    $runtimeDir = Join-Path $repoRoot 'small_model/runtime/hunyuan'
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $process = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-u', ('"' + $serverPath + '"'), '--port', $Port) `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeDir "server-$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $runtimeDir "server-$stamp.stderr.log")
    $process.Id | Set-Content -LiteralPath (Join-Path $runtimeDir "server-$Port.pid")
    Write-Output "Started PID $($process.Id); model is loading. Check http://127.0.0.1:$Port/health"
    exit 0
}
& $pythonPath $serverPath --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Hunyuan server exited with an error' }
