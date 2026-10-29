import reference as r
from certified_lp import solve
import json
for c,A,b in [([1,0],[[1,1]],[10]),([1],[[1],[1]],[1,2]),([1],[[1],[2]],[1,2])]:
 print(solve(c,A,b))
pool=r.ind.build_sample_pool()
for k,v in r.ind.TARGET_SCENARIOS.items():
 x=r.deada(pool,v);d=x['solver_diagnostics']; print(k,x['D'],x['降级'],d.get('optimality_verified'),d.get('relative_duality_gap'),d.get('baseline_certificate'))
