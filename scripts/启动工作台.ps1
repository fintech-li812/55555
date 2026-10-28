param([switch]$OpenBrowser)
$ErrorActionPreference='Stop'
$taskPython='D:\AI_Command_Workspace\runtime\cashflow_v1\Scripts\python.exe'
$taskEntry=Join-Path $PSScriptRoot 'mcp-server\app.py'
if (-not (Test-Path -LiteralPath $taskPython)) {throw 'Worktable Python runtime is missing.'}
if (-not (Test-Path -LiteralPath $taskEntry)) {throw 'Worktable entry is missing.'}
$taskListener=Get-NetTCPConnection -LocalPort 18766 -State Listen -ErrorAction SilentlyContinue
if ($taskListener) {
    Write-Output 'Port 18766 already has a listener. No duplicate process was started.'
    Write-Output 'http://127.0.0.1:18766'
    if ($OpenBrowser) {Start-Process 'http://127.0.0.1:18766'}
    exit 0
}
$taskLog=Join-Path $PSScriptRoot ('outbox\runtime\'+(Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $taskLog | Out-Null
$taskArgs=@('-B','-u',('"'+$taskEntry+'"'),'web')
$taskProc=Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLog 'stdout.txt') -RedirectStandardError (Join-Path $taskLog 'stderr.txt')
[pscustomobject]@{status='started_not_yet_verified';pid=$taskProc.Id;url='http://127.0.0.1:18766';log=$taskLog;automatic_database_write=$false} | ConvertTo-Json
if ($OpenBrowser) {Start-Process 'http://127.0.0.1:18766'}
