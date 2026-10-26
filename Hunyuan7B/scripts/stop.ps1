param([int]$Port = 8008)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pidPath = Join-Path $repoRoot "small_model/runtime/hunyuan/server-$Port.pid"
$serverPath = Join-Path $PSScriptRoot 'serve.py'
if (-not (Test-Path -LiteralPath $pidPath)) { throw 'No background PID file; stop a foreground server with Ctrl+C' }
$serverPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
if ($serverProcess) {
    if (-not $serverProcess.CommandLine.Contains($serverPath)) { throw 'PID belongs to a different process; refusing to stop it' }
    # A Windows venv launcher can own a second Python process with the same script.
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$serverPid" | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine.Contains($serverPath)
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $pidPath
Write-Output 'Stopped the recorded local model process.'
