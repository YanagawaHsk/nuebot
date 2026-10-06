$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先创建 .venv 并安装 requirements.txt 中的依赖，参阅 README。' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'account.json'))) { throw '请先运行 setup_local.py，填写本机账号。' }
Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8',('"' + (Join-Path $PSScriptRoot 'open_panel.pyw') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
