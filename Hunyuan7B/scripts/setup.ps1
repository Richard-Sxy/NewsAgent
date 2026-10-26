param([string]$Proxy = '')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pythonPath = Join-Path $repoRoot 'small_model/environments/hunyuan/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    py -3.11 -m venv (Join-Path $repoRoot 'small_model/environments/hunyuan')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 venv creation failed' }
}
& $pythonPath -m pip install 'huggingface-hub==0.36.0'
if ($LASTEXITCODE -ne 0) { throw 'Download dependency installation failed' }
& $pythonPath (Join-Path $PSScriptRoot 'download_torch.py')
if ($LASTEXITCODE -ne 0) { throw 'CUDA wheel download failed' }
& $pythonPath -m pip install (Join-Path $repoRoot 'small_model/runtime/hunyuan/wheels/torch-2.9.1+cu128-cp311-cp311-win_amd64.whl')
if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed' }
& $pythonPath -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency installation failed' }
$downloadArgs = @((Join-Path $PSScriptRoot 'download.py'))
if ($Proxy) { $downloadArgs += @('--proxy', $Proxy) }
& $pythonPath @downloadArgs
if ($LASTEXITCODE -ne 0) { throw 'Model download or hash verification failed' }
