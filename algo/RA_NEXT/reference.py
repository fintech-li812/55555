"""RA_NEXT 0.3: isolated statistical reference, never imported by production."""
import sys, math, random, json
from pathlib import Path
from bisect import bisect_left
from functools import lru_cache
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mcp-server/program_v1/src'))
from v8 import layers as baseline, indicators as ind
from v8.fusion import fuse
VERSION = 'ra-next-0.3'

@lru_cache(maxsize=256)
def null_distribution(n, probabilities, simulations=1999, seed=20261029):
    rng = random.Random(seed)
    cumulative = []
    for p in probabilities:
        cumulative.append(sum(probabilities[:len(cumulative)+1]))
    out = []
    for _ in range(simulations):
        counts = [0] * len(probabilities)
        for _ in range(n):
            counts[min(bisect_left(cumulative, rng.random()), len(counts)-1)] += 1
        out.append(sum((c-n*p)**2/(n*p) for c,p in zip(counts, probabilities)))
    return tuple(sorted(out))

def mc_test(counts, probabilities, simulations=1999):
    if len(counts)!=len(probabilities) or not counts or any(type(c) is not int or c<0 for c in counts) or any(not isinstance(p,(int,float)) or not math.isfinite(p) or p<=0 for p in probabilities) or abs(sum(probabilities)-1)>1e-10 or type(simulations) is not int or simulations<99:
        raise ValueError('invalid multinomial counts, probabilities or simulations')
    n = sum(counts)
    if n < 8:
        return {'available': False, 'reason': 'fewer than 8 usable observations', 'p': None}
    statistic = sum((c-n*p)**2/(n*p) for c,p in zip(counts, probabilities))
    null = null_distribution(n, tuple(probabilities), simulations)
    hits = len(null)-bisect_left(null, statistic-1e-12)
    p = (hits+1)/(len(null)+1)
    # Wilson interval measures Monte Carlo uncertainty, not business risk.
    ph = hits/len(null); z = 1.959963984540054; den = 1+z*z/len(null)
    center = (ph+z*z/(2*len(null)))/den
    width = z*math.sqrt(ph*(1-ph)/len(null)+z*z/(4*len(null)**2))/den
    return {'available': True, 'p': p, 'mc_ci': [max(0,center-width),min(1,center+width)], 'statistic': statistic, 'n': n}

def quality_gate(pool, *, alpha=0.05, simulations=1999):
    if not 0 < alpha < 1 or simulations < 99:
        raise ValueError('invalid alpha or simulation count')
    if not isinstance(pool,list) or any(not isinstance(s,dict) or not isinstance(s.get('values'),dict) for s in pool):
        return {'Q':None,'available':False,'reason':'invalid pool structure','明细':{}}
    details = {}; tests = []
    for key in ind.KEYS:
        vals = [s['values'].get(key) for s in pool]
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vals):
            return {'Q': None, 'available': False, 'reason': 'invalid numeric input', '明细': {}}
        first = [baseline.first_digit(v) for v in vals]
        last = [baseline.last_digit(v) for v in vals]
        a = mc_test([first.count(d) for d in range(1,10)], baseline.BENFORD, simulations)
        b = mc_test([last.count(d) for d in range(10)], [0.1]*10, simulations)
        details[key] = {'first': a, 'last': b, 'benford_风险数字': None}
        tests.extend([a,b])
    if not all(t['available'] for t in tests):
        return {'Q': None, 'available': False, 'reason': 'insufficient usable digits', '明细': details}
    # Holm controls familywise error under arbitrary dependence.
    ordered = sorted(range(len(tests)), key=lambda i: tests[i]['p'])
    rejected = set()
    for rank,i in enumerate(ordered):
        if tests[i]['p'] <= alpha/(len(tests)-rank): rejected.add(i)
        else: break
    return {'Q': round(1-len(rejected)/len(tests),4), 'available': True,
            '明细': details, 'alpha': alpha, 'adjustment': 'Holm', '版本': VERSION,
            'interpretation': 'digit-distribution compatibility only; not fraud probability',
            'applicability': 'Benford suitability must be justified separately for bounded ratios'}

def deada(pool, target):
    if not isinstance(pool,list) or any(not isinstance(s,dict) or type(s.get('label')) is not int or s['label'] not in (0,1) for s in pool):
        return {'D': None, '降级': True, 'reason': 'invalid pool or labels'}
    if not isinstance(target,dict):
        return {'D': None, '降级': True, 'reason': 'invalid target'}
    if len(pool)<8 or {s['label'] for s in pool}!={0,1}:
        return {'D': None, '降级': True, 'reason': 'insufficient two-class training data'}
    values = target.get('values', target)
    if not valid_values(values) or any(not valid_values(x.get('values',{})) or type(x.get('label')) is not int or x['label'] not in (0,1) for x in pool):
        return {'D': None, '降级': True, 'reason': 'invalid target, training values or labels'}
    if any(not isinstance(s['values'].get(k),(int,float)) or not math.isfinite(s['values'][k]) for s in pool for k in ind.KEYS):
        return {'D': None, '降级': True, 'reason': 'invalid training values'}
    result = diagnosed_deada(pool, values)
    if result['降级']: result['D'] = None
    result['版本'] = VERSION
    return result

def deada_stability(pool, target, repeats=30, seed=20261029):
    rng = random.Random(seed); draws=[]; weights=[]; failed=0
    groups = [[s for s in pool if s['label']==label] for label in (0,1)]
    for _ in range(repeats):
        sample = [rng.choice(g) for g in groups for _ in g]
        r = deada(sample,target)
        if r['降级']: failed+=1
        else: draws.append(r['D']); weights.append(r['权重'])
    def interval(xs):
        xs=sorted(xs)
        return [xs[int((len(xs)-1)*0.025)], xs[int((len(xs)-1)*0.975)]] if xs else None
    return {'D_bootstrap_interval': interval(draws), 'weight_intervals': {k: interval([w[k] for w in weights]) for k in weights[0]} if weights else {}, 'failed': failed, 'repeats': repeats, 'note': 'exploratory percentile stability, not guaranteed coverage'}

def isotonic_fit(scores, labels):
    grouped={}
    for x,y in zip(scores,labels):
        total,n=grouped.get(x,(0,0)); grouped[x]=(total+y,n+1)
    blocks=[]
    for x,(total,n) in sorted(grouped.items()):
        blocks.append([x,total,n])
        while len(blocks)>1 and blocks[-2][1]/blocks[-2][2]>blocks[-1][1]/blocks[-1][2]:
            b=blocks.pop(); a=blocks.pop(); blocks.append([b[0],a[1]+b[1],a[2]+b[2]])
    return blocks

def isotonic_predict(blocks,x):
    return next((s/n for end,s,n in blocks if x<=end),blocks[-1][1]/blocks[-1][2])

def _features(values):
    # Fixed digit surprise avoids fitting risk digits on validation observations.
    return [values[k] for k in ind.KEYS]+[-math.log(baseline.BENFORD[d-1]) if (d:=baseline.first_digit(values[k])) else 0.0 for k in ind.KEYS]

def _forest(pool,n_trees,seed):
    rng=random.Random(seed); X=[_features(s['values']) for s in pool]; y=[s['label'] for s in pool]
    models=[]; oob=[[] for _ in pool]
    for _ in range(n_trees):
        idx=[rng.randrange(len(pool)) for _ in pool]
        cols=sorted(rng.sample(range(30),max(1,int(math.sqrt(30)))))
        tree=baseline._Tree(4).fit([[X[i][j] for j in cols] for i in idx],[y[i] for i in idx])
        models.append((cols,tree))
        used=set(idx)
        for i in range(len(pool)):
            if i not in used: oob[i].append(tree.predict([X[i][j] for j in cols]))
    return models,oob

def _predict(models,values):
    x=_features(values)
    return sum(t.predict([x[j] for j in c]) for c,t in models)/len(models)

def benford_rf(pool,target,gate,n_trees=60,seed=20261027):
    if not isinstance(target,dict) or not isinstance(pool,list) or any(not isinstance(s,dict) for s in pool):
        return {'P':None,'树数':0,'available':False,'reason':'invalid pool or target'}
    if not valid_values(target.get('values',target)) or any(not valid_values(s.get('values',{})) or type(s.get('label')) is not int or s['label'] not in (0,1) for s in pool):
        return {'P':None,'树数':0,'available':False,'reason':'invalid values or labels'}
    if type(n_trees) is not int:
        return {'P':None,'树数':0,'available':False,'reason':'tree count must be integer'}
    groups=[[s for s in pool if s['label']==label] for label in (0,1)]
    if n_trees<1 or min(map(len,groups))<8:
        return {'P':None,'树数':0,'available':False,'reason':'insufficient trees or class samples'}
    rng=random.Random(seed); train=[]; calibration=[]
    for g in groups:
        g=g[:]; rng.shuffle(g); n=max(3,len(g)//4); calibration+=g[:n]; train+=g[n:]
    models,oob=_forest(train,n_trees,seed)
    scores=[_predict(models,s['values']) for s in calibration]
    blocks=sigmoid_fit(scores,[s['label'] for s in calibration])
    raw=_predict(models,target.get('values',target)); calibrated=sigmoid_predict(blocks,raw)
    valid=[(sum(v)/len(v),train[i]['label']) for i,v in enumerate(oob) if v]
    return {'P':round(calibrated,4),'P_raw':round(raw,4),'P_calibrated':round(calibrated,4),'树数':len(models),'available':True,
            'calibration_diagnostics':sigmoid_diagnostics(blocks,scores,[s['label'] for s in calibration]),
            'calibration_reliable':len(calibration)>=30 and sigmoid_diagnostics(blocks,scores,[s['label'] for s in calibration])['converged'],
            'calibration_min_n_policy':30,
            'calibration_n':len(calibration),'calibration':'held-out regularized sigmoid; small synthetic sample only',
            'OOB_brier':sum((p-y)**2 for p,y in valid)/len(valid) if valid else None,
            'OOB_coverage':len(valid)/len(train),'版本':VERSION,'class_weight':'none; natural training prevalence retained',
            'feature_sampling':'5 of 30 per tree; random-subspace forest, not per-node RF'}


def sigmoid_predict(parameters,p):
    a,b=parameters
    x=math.log(max(1e-4,min(1-1e-4,p))/(1-max(1e-4,min(1-1e-4,p))))
    z=max(-35,min(35,a*x+b))
    return 1/(1+math.exp(-z))

def sigmoid_fit(scores,labels):
    a,b=1.0,0.0
    for _ in range(1000):
        da,db=0.1*a,0.1*b
        for p,y in zip(scores,labels):
            x=math.log(max(1e-4,min(1-1e-4,p))/(1-max(1e-4,min(1-1e-4,p))))
            error=sigmoid_predict((a,b),p)-y
            da+=error*x/len(scores); db+=error/len(scores)
        a=max(0,a-0.05*da); b-=0.05*db
    return a,b


def valid_values(values):
    return isinstance(values,dict) and all(type(values.get(k)) in (int,float) and math.isfinite(values[k]) and abs(values[k]) <= 1e100 for k in ind.KEYS) and values['X10']>0


def checked_simplex(c,A,b,solver=None):
    x,obj,ok=(solver or baseline._simplex)(c,A,b)
    diagnostics={'solver_reported_success':ok, 'optimality_verified':False}
    if not ok or x is None:
        diagnostics['reason']='solver did not return a solution'
        return None,None,False,diagnostics
    if len(x)!=len(c) or any(not math.isfinite(v) for v in x) or obj is None or not math.isfinite(obj):
        diagnostics['reason']='nonfinite or malformed solution'
        return None,None,False,diagnostics
    residual=max((abs(sum(a*v for a,v in zip(row,x))-rhs)/(1+abs(rhs)+sum(abs(a*v) for a,v in zip(row,x))) for row,rhs in zip(A,b)),default=0)
    negative=max(0,-min(x,default=0)); computed=sum(a*v for a,v in zip(c,x))
    objective_error=abs(computed-obj)/(1+abs(computed))
    diagnostics.update(relative_primal_residual=residual,negative_variable=negative,objective_error=objective_error)
    good=residual<=1e-7 and negative<=1e-7 and objective_error<=1e-7
    diagnostics['reason']='primal feasibility checked; no dual optimality certificate' if good else 'solution violates constraints or objective consistency'
    return (x,obj,True,diagnostics) if good else (None,None,False,diagnostics)


def diagnosed_deada(pool,target):
    # Execute an isolated copy with a local solver binding, never mutate baseline globals.
    import inspect
    diagnostics={}
    def solve(c,A,b):
        from certified_lp import solve as certified_solve, certificate
        old_x,old_obj,old_ok,old_info=checked_simplex(c,A,b)
        x,obj,ok,info=certified_solve(c,A,b)
        diagnostics.update(info)
        diagnostics['baseline_objective']=old_obj
        if old_ok and ok:
            old_certificate=certificate(c,A,b,old_x,info['dual'])
            diagnostics['baseline_certificate']=old_certificate
            if old_certificate['optimality_verified']:
                diagnostics['baseline_preserved']=True
                return old_x,old_obj,True
        diagnostics['baseline_preserved']=False
        return x,obj,ok
    namespace=dict(baseline.__dict__)
    namespace['_simplex']=solve
    exec(compile(inspect.getsource(baseline.deada),'baseline_deada_with_local_diagnostics','exec'),namespace)
    result=namespace['deada'](pool,target)
    result['solver_diagnostics']=diagnostics
    return result

def sigmoid_diagnostics(parameters,scores,labels):
    a,b=parameters;da=.1*a;db=.1*b
    for p,y in zip(scores,labels):
        p=max(1e-4,min(1-1e-4,p));x=math.log(p/(1-p))
        error=sigmoid_predict(parameters,p)-y
        da+=error*x/len(scores);db+=error/len(scores)
    projected_a=da if a>0 else min(0,da)
    residual=max(abs(projected_a),abs(db))
    return {'projected_gradient':residual,'converged':residual<=1e-5,'tolerance':1e-5,'reason':'convex objective projected-gradient check; sample count is a separate policy gate'}


def assess_reference(pool,target,*,benford_applicable=False):
    gate=quality_gate(pool);da=deada(pool,target);rf=benford_rf(pool,target,gate)
    fused=fuse(gate['Q'] if benford_applicable else None,da['D'],rf['P'],d_reliable=not da['降级'],p_reliable=rf.get('calibration_reliable',False))
    return {'version':VERSION,'quality':gate,'deada':da,'rf':rf,'fusion':fused,'benford_applicable':benford_applicable,'external_actions':False}


if __name__ == '__main__':
    pool=ind.build_sample_pool()
    gate=quality_gate(pool)
    print(json.dumps({'version':VERSION,'quality':gate,'scenarios':{k:{'D':deada(pool,v),'P':benford_rf(pool,v,gate)} for k,v in ind.TARGET_SCENARIOS.items()}},ensure_ascii=False,indent=2))
