import json,sys,platform,hashlib
from pathlib import Path
import reference as r

def snapshot():
 pool=r.ind.build_sample_pool();gate=r.quality_gate(pool)
 out={'Q':gate['Q'],'scenarios':{}}
 for name,target in r.ind.TARGET_SCENARIOS.items():
  a=r.assess_reference(pool,target,benford_applicable=True)
  out['scenarios'][name]={'D':a['deada']['D'],'P':a['rf']['P'],'raw_P':a['rf']['P_raw'],'calibration_reliable':a['rf']['calibration_reliable'],'level':a['fusion']['等级'],'rule':a['fusion']['规则ID'],'D_certified':a['deada']['solver_diagnostics']['optimality_verified']}
 return out
if __name__=='__main__':
 a=snapshot()
 for _ in range(9):
  if snapshot()!=a:raise AssertionError('same-input determinism failed')
 text=json.dumps(a,ensure_ascii=False,sort_keys=True,separators=(',',':'))
 out={'version':r.VERSION,'python':platform.python_version(),'platform':platform.system(),'repeat_runs':10,'semantic_snapshot':a,'sha256':hashlib.sha256(text.encode()).hexdigest()}
 Path(sys.argv[1]).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(out,ensure_ascii=False))
