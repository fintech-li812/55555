from pathlib import Path
p=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT\reference.py')
s=p.read_text(encoding='utf-8').replace("VERSION = 'ra-next-0.1'", "VERSION = 'ra-next-0.2'")
a=s.index("if __name__ == '__main__':")
b=s.index('def isotonic_fit',a)
s=s[:a]+s[b:]
s=s.replace('result = baseline.deada(pool, values)', 'result = diagnosed_deada(pool, values)')
s=s.replace("    values = target.get('values', target)\n", "    values = target.get('values', target)\n    if not valid_values(values) or any(not valid_values(x.get('values',{})) or type(x.get('label')) is not int or x['label'] not in (0,1) for x in pool):\n        return {'D': None, '降级': True, 'reason': 'invalid target, training values or labels'}\n")
s=s.replace("    groups=[[s for s in pool if s['label']==label] for label in (0,1)]\n", "    if not valid_values(target.get('values',target)) or any(not valid_values(s.get('values',{})) or type(s.get('label')) is not int or s['label'] not in (0,1) for s in pool):\n        return {'P':None,'树数':0,'available':False,'reason':'invalid values or labels'}\n    if type(n_trees) is not int:\n        return {'P':None,'树数':0,'available':False,'reason':'tree count must be integer'}\n    groups=[[s for s in pool if s['label']==label] for label in (0,1)]\n")
s+='''

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
        x,obj,ok,info=checked_simplex(c,A,b)
        diagnostics.update(info)
        return x,obj,ok
    namespace=dict(baseline.__dict__)
    namespace['_simplex']=solve
    exec(compile(inspect.getsource(baseline.deada),'baseline_deada_with_local_diagnostics','exec'),namespace)
    result=namespace['deada'](pool,target)
    result['solver_diagnostics']=diagnostics
    return result

if __name__ == '__main__':
    pool=ind.build_sample_pool()
    gate=quality_gate(pool)
    print(json.dumps({'version':VERSION,'quality':gate,'scenarios':{k:{'D':deada(pool,v),'P':benford_rf(pool,v,gate)} for k,v in ind.TARGET_SCENARIOS.items()}},ensure_ascii=False,indent=2))
'''
p.write_text(s,encoding='utf-8')
