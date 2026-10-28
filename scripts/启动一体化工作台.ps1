<#
.SYNOPSIS
    一键拉起「统一工作台」依赖的两个后端进程，并打开已联通的页面。

.DESCRIPTION
    主站 public_server.py：页面 + 主 API（/api/*），并把 /v8/api/v8/* 反向代理到 V8。
    V8   v8_server.py  ：三层风控 API（/api/v8/*）。

    页面只从 http://127.0.0.1:<MainPort>/unified 这一个源发起请求，
    由主站内部完成转发，因此浏览器侧不产生跨域请求，后端不需要开 CORS。

    数据库配置从 deploy/.env 读取（仅在子进程环境中展开，不写入用户环境、不打印口令）。
    数据库不可达时计算与评估照常工作，保存/回查/留痕会如实报错，不假装成功。
#>
param(
    [int]$MainPort = 8000,
    [int]$V8Port = 8010,
    [int]$DbPort = 3307,
    # 可选：一并拉起本机隔离 MySQL（deploy/.mysqldata）。
    # 不指定也能正常用：计算、评估、页面全部可用，只有保存/回查/留痕会如实报「数据库不可用」。
    [switch]$WithDatabase,
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src     = Join-Path $Root 'mcp-server\program_v1\src'
$EnvFile = Join-Path $Root 'deploy\.env'
$LogRoot = Join-Path $Root 'outbox\runtime'

# ---------------------------------------------------------------- 解释器
# 优先复用项目既有虚拟环境；找不到时退回 PATH 上的 python。
$Python = 'D:\AI_Command_Workspace\runtime\cashflow_v1\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Python) { throw '未找到可用的 Python 解释器。' }
}

foreach ($f in @((Join-Path $Src 'public_server.py'), (Join-Path $Src 'v8_server.py'))) {
    if (-not (Test-Path -LiteralPath $f)) { throw "缺少文件：$f" }
}

# ------------------------------------------------------- 读取 deploy/.env
# 与生产入口 deploy/serve.py 行为一致：KEY=VALUE，忽略注释与空行。
$FileEnv = @{}
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $pair = $line -split '=', 2
        $FileEnv[$pair[0].Trim()] = $pair[1].Trim()
    }
} else {
    Write-Output "未找到 $EnvFile ：数据库连接不可用，计算与评估仍可运行。"
}

if (-not (Test-Path -LiteralPath $LogRoot)) {
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

function Wait-Port {
    param([int]$Port, [int]$Seconds = 40)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', $Port)
            return $true
        } catch {
            Start-Sleep -Milliseconds 400
        } finally {
            $client.Dispose()
        }
    }
    return $false
}

function Start-Backend {
    param([string]$Name, [string]$Script, [int]$Port, [hashtable]$ChildEnv)

    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        $pids = ($busy.OwningProcess | Select-Object -Unique) -join ','
        # 用 Write-Host：Write-Output 会把这段文字并进函数返回值，污染调用处
        Write-Host "$Name ：端口 $Port 已在监听（pid $pids），复用现有进程，不重复启动。"
        return $true
    }

    # 用 .NET 直接起进程，而不是 Start-Process：
    # 1) 只在子进程的 EnvironmentVariables 上设值，完全不动当前 shell 的环境；
    # 2) 规避 PS 5.1 在环境里同时存在 HTTPS_PROXY / https_proxy（仅大小写不同）时
    #    Start-Process 直接抛「已添加项…字典中的关键字」的已知缺陷。
    $out = Join-Path $LogRoot "$stamp-$Name-stdout.txt"
    $err = Join-Path $LogRoot "$stamp-$Name-stderr.txt"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $env:ComSpec
    # 环境变量用 cmd 的 set 注入，而不是 .NET 的 ProcessStartInfo.EnvironmentVariables：
    # 后者会把当前进程环境整个快照一遍，当环境里同时存在 HTTPS_PROXY 与 https_proxy
    # （仅大小写不同）时，.NET Framework 4.x 会抛「已添加项…字典中的关键字」，
    # PS 5.1 的 Start-Process 踩的是同一个坑，这里一并绕开。
    # 必须写成 set "K=V"：裸写 set K=V 时，&& 前的那个空格会被当成值的一部分
    # （WORKBENCH_HOST 变成 "127.0.0.1 "，Python 把它当主机名去查 DNS，直接
    # getaddrinfo failed）。加引号后既没有多余空格，也不怕值里有 & | > 等字符。
    $sets = @()
    foreach ($k in $ChildEnv.Keys) { $sets += ('set "' + $k + '=' + [string]$ChildEnv[$k] + '"') }
    # 路径含空格时用 cmd 的双引号套路：整段外面再套一对引号，
    # 于是首尾各出现两个相邻的引号，路径本身的引号保持单个。
    $inner = if ($sets.Count -gt 0) { ($sets -join ' && ') + ' && ' } else { '' }
    $inner += '"' + $Python + '" -B -u "' + $Script + '" > "' + $out + '" 2> "' + $err + '"'
    $psi.Arguments = '/c "' + $inner + '"'
    $psi.WorkingDirectory = $Src
    $psi.UseShellExecute  = $false
    $psi.CreateNoWindow   = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null

    if (Wait-Port $Port) {
        Write-Host "$Name ：已启动 127.0.0.1:$Port（日志 $out）"
        return $true
    }
    Write-Warning "$Name ：端口 $Port 在 40 秒内未开始监听，请查看 $err"
    return $false
}

function Start-StandaloneMySql {
    # 独立的 MySQL 实例：独立 datadir、独立端口，不碰本机已有的 MySQL84 服务。
    $busy = Get-NetTCPConnection -LocalPort $DbPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "mysql：端口 $DbPort 已在监听，复用现有实例。"
        return $true
    }
    $found = Get-ChildItem 'C:\Program Files\MySQL' -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'bin\mysqld.exe' } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $found) {
        Write-Warning "未找到 mysqld.exe，隔离库未启动；保存/回查/留痕将如实不可用。"
        return $false
    }
    # 上次若被强杀，InnoDB 崩溃恢复会把「创建 undo 表空间」这条数据字典记录回滚掉，
    # 于是本次启动又要重建，却撞上次残留的文件，报
    # "Can't create UNDO tablespace ... already exists"，陷入每次都起不来的死循环。
    # 对策：启动前先把 undo 目录清空（旧文件归档），让 InnoDB 干净重建。
    # 只动 undo 表空间，不碰 datadir，表数据不受影响。
    $cnfPath = Join-Path $Root 'deploy\mysqld-standalone.cnf'
    $undoDir = ''
    foreach ($line in [System.IO.File]::ReadAllLines($cnfPath)) {
        if ($line -match '^\s*innodb-undo-directory\s*=\s*(.+?)\s*$') { $undoDir = $Matches[1] }
    }
    if ($undoDir -and (Test-Path -LiteralPath $undoDir)) {
        $stale = @(Get-ChildItem -LiteralPath $undoDir -File -ErrorAction SilentlyContinue)
        if ($stale.Count -gt 0) {
            # 固定目录 + 覆盖，磁盘占用恒定；最初的原始 undo 另存于 .undo-backup-20261028
            $keep = Join-Path $Root 'deploy\.undo-backup\last'
            New-Item -ItemType Directory -Path $keep -Force | Out-Null
            foreach ($f in $stale) {
                try { Move-Item -LiteralPath $f.FullName -Destination (Join-Path $keep $f.Name) -Force } catch {}
            }
            Write-Host "mysql：归档 $($stale.Count) 个旧 undo 文件，本次将重建 undo 表空间。"
        }
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName         = $found
    $psi.Arguments        = '--defaults-file="' + (Join-Path $Root 'deploy\mysqld-standalone.cnf') + '"'
    $psi.UseShellExecute  = $false
    $psi.CreateNoWindow   = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null

    # InnoDB 要做崩溃恢复，给足时间，不要用启动脚本的 40 秒上限。
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 800
        if (Get-NetTCPConnection -LocalPort $DbPort -State Listen -ErrorAction SilentlyContinue) {
            Write-Host "mysql：已启动 127.0.0.1:$DbPort"
            return $true
        }
    }
    Write-Warning "mysql：120 秒内未在 $DbPort 开始监听，保存/回查/留痕将如实不可用。"
    return $false
}

# 数据库与应用共用的键；缺哪个都不会中断启动，只影响依赖存储的功能。
$dbKeys = @('CF_DB_HOST', 'CF_DB_PORT', 'CF_DB_NAME', 'CF_DB_USER', 'CF_DB_PASSWORD',
            'CF_TENANT_ID', 'CF_COMPANY_ID', 'CF_RESULT_FILE', 'CF_RESULT_SHA256',
            'CF_QA_MODE', 'CF_ALLOW_FIXTURE', 'CF_QWEN_BOUND')

$v8Env = @{}
$mainEnv = @{}
foreach ($k in $dbKeys) {
    if ($FileEnv.ContainsKey($k) -and $FileEnv[$k]) { $v8Env[$k] = $FileEnv[$k]; $mainEnv[$k] = $FileEnv[$k] }
}

# V8 自己按 CF_V8_PORT 监听；主站按同一个值定位上游，两者必须一致。
$v8Env['CF_V8_PORT'] = "$V8Port"

$mainEnv['CF_V8_PORT']             = "$V8Port"
$mainEnv['WORKBENCH_HOST']         = '127.0.0.1'
$mainEnv['WORKBENCH_PORT']         = "$MainPort"
# 必须与浏览器地址栏完全一致，否则请求会被 Host 白名单拒绝（403 host_not_allowed）。
$mainEnv['WORKBENCH_ALLOWED_HOSTS'] = "127.0.0.1:$MainPort,localhost:$MainPort"
# 直接以模块方式运行时，需要让「config / service / program_*」等顶层模块可被导入。
$mainEnv['PYTHONPATH']             = $Src

# -WithDatabase 时先把隔离库拉起来，让两个服务启动时就能连上。
$dbOk = $false
if ($WithDatabase) { $dbOk = Start-StandaloneMySql }

# 端口以脚本参数为准，保证应用连的就是我们刚拉起的那个实例。
if ($FileEnv.ContainsKey('CF_DB_NAME') -and $FileEnv['CF_DB_NAME']) {
    $v8Env['CF_DB_PORT']   = "$DbPort"
    $mainEnv['CF_DB_PORT'] = "$DbPort"
}

# V8 是上游，先起来，避免主站刚启动就有请求打到还没监听的端口。
$v8Ok   = Start-Backend -Name 'v8'   -Script (Join-Path $Src 'v8_server.py')     -Port $V8Port   -ChildEnv $v8Env
$mainOk = Start-Backend -Name 'main' -Script (Join-Path $Src 'public_server.py') -Port $MainPort -ChildEnv $mainEnv

$url = "http://127.0.0.1:$MainPort/unified"
if ($v8Ok -and $mainOk) {
    Write-Output ''
    Write-Output "一体化工作台已联通：$url"
    Write-Output "  · 主站 API ：http://127.0.0.1:$MainPort/api/status"
    Write-Output "  · V8   API ：http://127.0.0.1:$MainPort/v8/api/v8/industries（经主站代理到 $V8Port）"
    if ($OpenBrowser) { Start-Process $url }
} else {
    Write-Warning "两个进程未全部就绪，页面可能只能显示内置快照。"
}

[pscustomobject]@{
    status               = if ($v8Ok -and $mainOk) { 'started_and_listening' } else { 'partially_started' }
    unified_url          = $url
    main_port            = $MainPort
    v8_port              = $V8Port
    v8_listening         = $v8Ok
    main_listening       = $mainOk
    database_port        = $DbPort
    database_listening   = if ($WithDatabase) { $dbOk } else { 'not_requested' }
    automatic_db_write   = $false
    logs                 = $LogRoot
}
