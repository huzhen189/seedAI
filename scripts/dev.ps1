# scripts/dev.ps1
# SeedAI 本地开发前后端统一启停 (Windows / PowerShell 原生, PID 空间与任务管理器一致)
#
# 为什么用 PowerShell 而不是 git-bash:
#   git-bash(msys) 的 kill/taskkill 看到的 PID 是虚拟 PID, 与 Windows 真实 PID 不一致,
#   直接杀会"杀不干净"(报找不到 / 子进程变孤儿)。PowerShell 是 Windows 原生, PID 真实,
#   且能递归杀掉【整棵进程树】(含 uvicorn reloader / vite esbuild 子进程), 不留孤儿。
#
# 用法 (在 git-bash 里用 .\scripts\dev.bat 包装, 或直接在 PowerShell 跑):
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 start
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 stop
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 restart
#   # 指定端口(默认后端 7103 / 前端 7104, 避开历史僵尸 7101):
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 start -BackendPort 7101 -FrontendPort 7104
#   # 单独清理某个历史僵尸端口(例如遗留的 7101):
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 stop  -BackendPort 7101 -FrontendPort 7100

param(
  [ValidateSet('start', 'stop', 'restart')][string]$Command = 'start',
  [int]$BackendPort = 7103,
  [int]$FrontendPort = 7104
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$script:_writers = @()   # 持有 Start-Background 创建的 StreamWriter, 供事件回调写入日志
$ROOT = Split-Path $MyInvocation.MyCommand.Path | Split-Path
$PY   = "C:\Users\zhenhu\.workbuddy\binaries\python\envs\seedai-biz\Scripts\python.exe"
$NODE = "C:\Users\zhenhu\.workbuddy\binaries\node\versions\22.22.2\node.exe"
$PID_DIR       = Join-Path $ROOT "scripts\.pids"
$BACKEND_PID   = Join-Path $PID_DIR "backend_$BackendPort.pid"
$FRONTEND_PID  = Join-Path $PID_DIR "frontend_$FrontendPort.pid"
$BACKEND_LOG   = Join-Path $ROOT "logs\backend_$BackendPort.log"
$BACKEND_ERR   = Join-Path $ROOT "logs\backend_$BackendPort.err"
$FRONTEND_LOG  = Join-Path $ROOT "logs\frontend_$FrontendPort.log"
$FRONTEND_ERR  = Join-Path $ROOT "logs\frontend_$FrontendPort.err"

# 按端口反查【真实 Windows PID】(PowerShell 原生, 与任务管理器一致)
function Get-PidByPort($port) {
  try {
    (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).OwningProcess |
      Where-Object { $_ -and $_ -ne 0 } | Sort-Object -Unique
  } catch { @() }
}

# 收集某 PID 的整棵进程树(含所有子进程), 基于 Win32_Process.ParentProcessId 递归
function Get-ChildTree($rootPid) {
  $tree = [System.Collections.Generic.List[int]]::new()
  $tree.Add($rootPid)
  try {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $changed = $true
    while ($changed) {
      $changed = $false
      foreach ($pr in $procs) {
        if ($pr.ParentProcessId -in $tree -and $pr.ProcessId -notin $tree) {
          $tree.Add($pr.ProcessId); $changed = $true
        }
      }
    }
  } catch {}
  return $tree
}

# 优雅优先 + 强制杀树兜底:
#   1) Stop-Process 先发退出信号(给 uvicorn/vite 机会优雅收尾, 释放端口)
#   2) 3s 后 Stop-Process -Force 强杀整棵进程树(父 + 所有子进程, 不留孤儿)
function Stop-Tree($pidv) {
  if (-not $pidv -or $pidv -eq 0) { return }
  $tree = Get-ChildTree $pidv
  Write-Host "  -> 优雅退出进程树 PID(s): $($tree -join ',')"
  foreach ($k in $tree) {
    try { Stop-Process -Id $k -ErrorAction SilentlyContinue } catch {}
  }
  Start-Sleep -Seconds 3
  foreach ($k in $tree) {
    try { Stop-Process -Id $k -Force -ErrorAction SilentlyContinue } catch {}
  }
  Start-Sleep -Seconds 1
}

function Stop-Service($port, $pidFile) {
  Write-Host "==> 停止占用 $port 的进程 ..."
  if (Test-Path $pidFile) {
    $p = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if ($p) { Stop-Tree $p }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
  }
  # 端口反查兜底: 覆盖 PID 文件丢失 / 孤儿进程 / 他人手动起的实例
  Get-PidByPort $port | ForEach-Object { Stop-Tree $_ }

  $left = Get-PidByPort $port
  if ($left) {
    $alive = @(); $ghost = @()
    foreach ($lp in $left) {
      if (Get-Process -Id $lp -ErrorAction SilentlyContinue) { $alive += $lp }
      else { $ghost += $lp }
    }
    if ($alive) { Write-Warning "  [!] 端口 $port 仍被活进程占用: $($alive -join ',')" }
    if ($ghost) {
      Write-Warning "  [!] 端口 $port 残留孤儿 socket (PID $($ghost -join ',') 进程已退出但未释放端口)."
      Write-Warning "      这是之前强杀留下的残留, 已无进程可杀. 建议: 换端口, 或管理员执行 'netsh int ipv4 reset' 后重启."
    }
  } else {
    Write-Host "  -> 端口 $port 已释放"
  }
}

function Start-Background($exe, $argList, $wd, $outLog, $errLog) {
  # 用底层 .NET Process.Start 而非 Start-Process: 规避 PS5.1 在系统存在 Path/PATH 重复
  # 环境变量键时构造环境字典抛 "已添加重复键" 的 bug; 同时可拿 PID 并分别重定向 stdout/stderr。
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $exe
  $psi.Arguments = ($argList | ForEach-Object { '"{0}"' -f ($_ -replace '"', '\"') }) -join ' '
  $psi.WorkingDirectory = $wd
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  # 用脚本作用域持有 StreamWriter(避免事件回调闭包丢失引用被 GC), 通过 Register-ObjectEvent
  # 把 .NET 数据到达事件桥接到 PowerShell 引擎事件队列, 引擎在空闲(Start-Sleep 期间)泵事件写入文件。
  $w = New-Object System.IO.StreamWriter($outLog, $false, [System.Text.Encoding]::UTF8)
  $e = New-Object System.IO.StreamWriter($errLog, $false, [System.Text.Encoding]::UTF8)
  $w.AutoFlush = $true; $e.AutoFlush = $true
  $script:_writers += $w, $e
  Register-ObjectEvent -InputObject $p -EventName OutputDataReceived -Action {
    if ($null -ne $EventArgs.Data) { $script:_writers[0].WriteLine($EventArgs.Data) }
  } | Out-Null
  Register-ObjectEvent -InputObject $p -EventName ErrorDataReceived -Action {
    if ($null -ne $EventArgs.Data) { $script:_writers[1].WriteLine($EventArgs.Data) }
  } | Out-Null
  $p.BeginOutputReadLine(); $p.BeginErrorReadLine()
  return $p.Id
}

function Stop-All {
  Stop-Service $BackendPort  $BACKEND_PID
  Stop-Service $FrontendPort $FRONTEND_PID
}

function Start-All {
  Stop-All  # 启动前先确保目标端口干净, 避免冲突
  New-Item -ItemType Directory -Force -Path $PID_DIR,
    (Split-Path $BACKEND_LOG), (Split-Path $FRONTEND_LOG) | Out-Null

  $env:VITE_API_TARGET = "http://127.0.0.1:$BackendPort"   # 子进程(.NET Process.Start)继承此环境变量

  Write-Host "==> 启动后端 (:$BackendPort) ..."
  $bArgs = @("-u", "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
             "--host", "0.0.0.0", "--port", $BackendPort, "--log-level", "info")
  $bPid = Start-Background $PY $bArgs $ROOT $BACKEND_LOG $BACKEND_ERR
  $bPid | Out-File $BACKEND_PID
  Write-Host "    后端 PID=$bPid  日志=$BACKEND_LOG"

  Write-Host "==> 启动前端 (:$FrontendPort -> 后端 :$BackendPort) ..."
  $fArgs = @("node_modules/vite/bin/vite.js", "--port", $FrontendPort, "--host")
  $fPid = Start-Background $NODE $fArgs (Join-Path $ROOT "frontend") $FRONTEND_LOG $FRONTEND_ERR
  $fPid | Out-File $FRONTEND_PID
  Write-Host "    前端 PID=$fPid  日志=$FRONTEND_LOG"

  Write-Host "==> 等待后端健康检查 ..."
  for ($i = 1; $i -le 20; $i++) {
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$BackendPort/health" `
              -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
      if ($r.StatusCode -eq 200) { Write-Host "    后端 /health = 200 OK"; break }
    } catch {}
    # Wait-Event 会泵 PowerShell 引擎事件队列, 使 Register-ObjectEvent 的日志写入在等待期间落盘
    Wait-Event -Timeout 1 -ErrorAction SilentlyContinue | Out-Null
  }
  Write-Host ""
  Write-Host "完成。前端访问: http://localhost:$FrontendPort"
  Write-Host "重启: powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 restart"
  Write-Host "停止: powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 stop"
}

switch ($Command) {
  'start'   { Start-All }
  'stop'    { Stop-All }
  'restart' { Stop-All; Start-All }
}

exit 0
