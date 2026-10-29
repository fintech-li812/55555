import reference as r
import random,json,time
from pathlib import Path
pool=r.ind.build_sample_pool(); gate=r.baseline.quality_gate(pool)
out={'synthetic_only':True,'scenarios':{}}
start=time.perf_counter(); q=r.quality_gate(pool); out['Q_new']=q['Q'];out['Q_old']=gate['Q'];out['Q_seconds']=time.perf_counter()-start
for name,target in r.ind.TARGET_SCENARIOS.items():
 start=time.perf_counter(); old=r.baseline.benford_rf(pool,target['values'],gate); oldtime=time.perf_counter()-start
 start=time.perf_counter(); new=r.benford_rf(pool,target,gate); newtime=time.perf_counter()-start
 d=r.deada(pool,target)
 out['scenarios'][name]={'old_P':old['P'],'new':new,'D':d['D'],'old_fuse':r.fuse(gate['Q'],d['D'],old['P'])['等级'],'new_fuse':r.fuse(q['Q'],d['D'],new['P'])['等级'],'old_P_seconds':oldtime,'new_P_seconds':newtime}
rng=random.Random(914731); truth=[]; oldps=[]; newps=[]
for i in range(80):
 label=i%2; v=r.ind._base_values(rng,label==0); truth.append(label)
 oldps.append(r.baseline.benford_rf(pool,v,gate)['P']);newps.append(r.benford_rf(pool,v,gate)['P'])
def metrics(ps):
 bins=[]
 for i in range(10):
  ids=[j for j,p in enumerate(ps) if min(int(p*10),9)==i]
  if ids: bins.append({'n':len(ids),'prediction':sum(ps[j] for j in ids)/len(ids),'observed':sum(truth[j] for j in ids)/len(ids)})
 return {'brier':sum((p-y)**2 for p,y in zip(ps,truth))/len(ps),'ece':sum(b['n']*abs(b['prediction']-b['observed']) for b in bins)/len(ps),'reliability':bins}
out['independent_synthetic_test']={'n':80,'seed':914731,'baseline':metrics(oldps),'candidate':metrics(newps)}
out['D_stability']=r.deada_stability(pool,r.ind.TARGET_SCENARIOS['edge'],30)
Path(__file__).with_name('results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(out,ensure_ascii=False,indent=2))
