from pathlib import Path
import subprocess,sys,json,hashlib,platform
root=Path(__file__).resolve().parent
result=subprocess.run([sys.executable,str(root.parents[1]/'mcp-server/program_v1/tests/test_fusion_rules.py')],capture_output=True,text=True,encoding='utf-8',errors='replace')
(root/'fusion_regression_v03.txt').write_text(result.stdout+result.stderr,encoding='utf-8')
manifest={}
for p in [root.parents[1]/'mcp-server/program_v1/src/v8/layers.py',root.parents[1]/'mcp-server/program_v1/src/v8/fusion.py',root.parents[1]/'mcp-server/program_v1/src/v8/indicators.py',root/'reference.py',root/'certified_lp.py',root/'decision_policy.py']:
 manifest[str(p.relative_to(root.parents[1]))]=hashlib.sha256(p.read_bytes()).hexdigest()
a=json.loads((root/'determinism_windows_v03.json').read_text(encoding='utf-8'));b=json.loads((root/'determinism_linux_v03.json').read_text(encoding='utf-8'))
report={'baseline_fusion_returncode':result.returncode,'windows_python':a['python'],'linux_python':b['python'],'same_semantic_snapshot':a['semantic_snapshot']==b['semantic_snapshot'],'same_snapshot_sha256':a['sha256']==b['sha256'],'files':manifest,'note':'source hashes pin observed baseline; no runtime dependency installation'}
(root/'verification_v03.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False));print((result.stdout+result.stderr)[-500:])
if result.returncode or not report['same_semantic_snapshot']:raise SystemExit(1)
