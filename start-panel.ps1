param(
    [string]$PythonPath = '',
    [ValidateSet('local','remote')][string]$Mode = 'local',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'

function Invoke-HiddenPythonProbe {
    param([string]$Executable,[string[]]$Arguments)
    $probeOutput = [System.IO.Path]::GetTempFileName()
    $probeError = [System.IO.Path]::GetTempFileName()
    try {
        $probeProcess = Start-Process -FilePath $Executable -ArgumentList $Arguments -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $probeOutput -RedirectStandardError $probeError
        if ($probeProcess.ExitCode -ne 0) { throw 'Python 检查失败。请使用 Python 3.11 或更新版本，并按 requirements.txt 安装本机模式需要的依赖。' }
        $probeText = Get-Content -LiteralPath $probeOutput -Raw
        if (-not $probeText) { throw 'Python 未返回检查结果，请指定可用的 Python 安装目录。' }
        return $probeText | ConvertFrom-Json
    } finally {
        Remove-Item -LiteralPath $probeOutput,$probeError -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-PanelPython {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) { throw '指定的 Python 不存在。请检查 -PythonPath 或 NUEBOT_PYTHON。' }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    foreach ($relative in '.venv\Scripts\pythonw.exe','.venv\Scripts\python.exe') {
        $candidatePath = Join-Path $PSScriptRoot $relative
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) { return $candidatePath }
    }
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        try {
            $resolvedPython = Invoke-HiddenPythonProbe -Executable $pythonLauncher.Source -Arguments @('-3','-c', '"import sys,json; print(json.dumps(sys.executable))"')
            if ($resolvedPython -is [string] -and (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { return $resolvedPython }
        } catch { }
    }
    foreach ($commandName in 'python.exe','python3.exe') {
        $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') { return $pythonCommand.Source }
    }
    throw '未找到 Python。请指定 -PythonPath、设置 NUEBOT_PYTHON，或安装 Python 3.11 及以上版本。'
}

try {
    Set-Location -LiteralPath $PSScriptRoot
    $requestedPython = if ($PythonPath) { $PythonPath } else { $env:NUEBOT_PYTHON }
    $panelPython = Resolve-PanelPython -Requested $requestedPython
    $probePython = $panelPython
    if ([System.IO.Path]::GetFileName($probePython) -ieq 'pythonw.exe') {
        $consolePython = Join-Path ([System.IO.Path]::GetDirectoryName($probePython)) 'python.exe'
        if (-not (Test-Path -LiteralPath $consolePython -PathType Leaf)) { throw 'pythonw.exe 所在目录缺少 python.exe，无法检查运行环境。' }
        $probePython = $consolePython
    }
    $probeCode = "import sys,json; assert sys.version_info >= (3,11), 'Python 3.11 required'; print(json.dumps({'version':list(sys.version_info[:3])}))"
    if ($Mode -eq 'local') {
        $probeCode = "import sys,json; assert sys.version_info >= (3,11), 'Python 3.11 required'; import websockets,PIL; from PIL import Image; assert websockets.__version__ == '15.0.1' and 11 <= int(PIL.__version__.split('.')[0]) < 13, 'requirements.txt dependencies required'; print(json.dumps({'version':list(sys.version_info[:3]),'websockets':websockets.__version__,'pillow':PIL.__version__}))"
    }
    $probeResult = Invoke-HiddenPythonProbe -Executable $probePython -Arguments @('-X','utf8','-c',('"' + $probeCode + '"'))
    if ($CheckOnly) {
        Write-Output ('Python ' + ($probeResult.version -join '.') + ' 检查通过；模式：' + $Mode + '。未启动服务或打开网页。')
        return
    }
    if ($Mode -eq 'local' -and -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'account.json'))) { throw '请先运行 setup_local.py，填写本机账号。' }
    Start-Process -FilePath $panelPython -ArgumentList @('-X','utf8',('"' + (Join-Path $PSScriptRoot 'open_panel.pyw') + '"'),'--mode',$Mode) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
} catch {
    $launcherError = $_.Exception.Message
    if (-not $CheckOnly) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.MessageBox]::Show($launcherError,'鵺 · 统一控制中心',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
        } catch { }
    }
    Write-Error $launcherError
    exit 1
}
