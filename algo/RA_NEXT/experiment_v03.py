import sys,json,random,time,statistics,platform,inspect
from pathlib import Path
import reference as r
import certified_lp as lp
from scipy.optimize import linprog
import scipy
from decision_policy import threshold_policy,unified_view

def main():
 rng=random.Random(20261030);checks=[]
 for case in range(30):
  m,n=5,4
  base=[[rng.uniform(.1,3) for _ in range(n)] for _ in range(m)]
  A=[row+[float(i==j) for j in range(m)] for i,row in enumerate(base)]
  b=[rng.uniform(2,10) for _ in range(m)];c=[rng.uniform(-1,3) for _ in range(n)]+[0]*m
  x,obj,ok,info=lp.solve(c,A,b);oracle=linprog([-v for v in c],A_eq=A,b_eq=b,method='highs')
  error=abs(obj+oracle.fun) if ok and oracle.success else None
  checks.append({'case':case,'certified':ok,'oracle_success':oracle.success,'objective_error':error})
 pool=r.ind.build_sample_pool();target=r.ind.TARGET_SCENARIOS['edge']['values'];captured={}
 def capture(c,A,b):
  captured.update(c=c,A=A,b=b);return r.baseline._simplex(c,A,b)
 namespace=dict(r.baseline.__dict__);namespace['_simplex']=capture
 exec(inspect.getsource(r.baseline.deada),namespace);namespace['deada'](pool,target)
 x,obj,ok,info=lp.solve(captured['c'],captured['A'],captured['b'])
 oracle=linprog([-v for v in captured['c']],A_eq=captured['A'],b_eq=captured['b'],method='highs')
 dea={'certified':ok,'objective_error':abs(obj+oracle.fun),'objective':obj,'highs_objective':-oracle.fun,'certificate':info}
 timing={}
 # Warm cache explicitly; cold Q is measured separately.
 r.null_distribution.cache_clear();start=time.perf_counter();r.quality_gate(pool);cold=time.perf_counter()-start
 for scenario in r.ind.TARGET_SCENARIOS:
  target=r.ind.TARGET_SCENARIOS[scenario]
  old=[];new=[]
  for _ in range(5):
   start=time.perf_counter();gate=r.baseline.quality_gate(pool);da=r.baseline.deada(pool,target['values']);p=r.baseline.benford_rf(pool,target['values'],gate);r.fuse(gate['Q'],da['D'],p['P']);old.append(time.perf_counter()-start)
   start=time.perf_counter();r.assess_reference(pool,target,benford_applicable=True);new.append(time.perf_counter()-start)
  timing[scenario]={'baseline_median':statistics.median(old),'candidate_warm_median':statistics.median(new),'ratio':statistics.median(new)/statistics.median(old)}
 h=[{'month':f'2025-{i:02d}','ratio':i/3} for i in range(1,13)]
 result={'version':r.VERSION,'python':platform.python_version(),'scipy':scipy.__version__,'random_LP_checks':checks,'dea_oracle':dea,'timing':timing,'cold_Q_seconds':cold,'threshold_demo':threshold_policy(h,2.5,1.5,'2026-01'),'unified_demo':unified_view('绿灯','绿灯',.3,temporal_validated=True),'synthetic_only':True}
 Path(__file__).with_name('results_v03.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'LP_passed':sum(x['certified'] and x['oracle_success'] and x['objective_error']<1e-7 for x in checks),'LP_total':len(checks),'DEA_objective_error':dea['objective_error'],'timing':timing,'cold_Q_seconds':cold},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
