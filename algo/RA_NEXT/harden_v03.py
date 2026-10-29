from pathlib import Path
p=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT\reference.py')
s=p.read_text(encoding='utf-8')
s=s.replace("                return old_x,old_obj,True", "                diagnostics['baseline_preserved']=True\n                return old_x,old_obj,True")
s=s.replace("            'calibration_n':len(calibration)","            'calibration_diagnostics':sigmoid_diagnostics(blocks,scores,[s['label'] for s in calibration]),\n            'calibration_reliable':len(calibration)>=30 and sigmoid_diagnostics(blocks,scores,[s['label'] for s in calibration])['converged'],\n            'calibration_min_n_policy':30,\n            'calibration_n':len(calibration)")
s=s.replace("if __name__ == '__main__':",'''def sigmoid_diagnostics(parameters,scores,labels):
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


if __name__ == '__main__':''')
p.write_text(s,encoding='utf-8')
