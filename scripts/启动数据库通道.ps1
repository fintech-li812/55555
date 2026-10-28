param([string]$KeyPath='C:\Users\33847\WorkBuddy\2026-10-15-20-06-39\deploy\keys\site_ed25519')
$ErrorActionPreference='Stop'
$taskScript='D:\AI_Command_Workspace\交付\CX-QA-RECOVER-001\Codex\v1\start_qa_tunnel.ps1'
if (-not (Test-Path -LiteralPath $taskScript)) {throw 'Previously verified tunnel launcher is missing.'}
& $taskScript -KeyPath $KeyPath
# Manual only. Never installs a scheduled task or exposes MySQL to the public network.
