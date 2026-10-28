from pathlib import Path
from importlib.metadata import version,PackageNotFoundError
import json
ROOT=Path(__file__).resolve().parent
rows=[];missing=[];mismatched=[]
for line in (ROOT/'requirements.lock.txt').read_text().splitlines():
    if '==' not in line:continue
    name,want=line.strip().split('==',1)
    try:actual=version(name)
    except PackageNotFoundError:actual=None;missing.append(name)
    rows.append(dict(name=name,expected=want,actual=actual))
    if actual is not None and actual!=want:mismatched.append(name)
report=dict(packages=len(rows),missing=missing,mismatched=mismatched,details=rows)
with (ROOT/'evidence/dependencies.json').open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
print(json.dumps({k:v for k,v in report.items() if k!='details'}))
raise SystemExit(2 if missing or mismatched else 0)
