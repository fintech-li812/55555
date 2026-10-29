import json,random,time,statistics
from pathlib import Path
import reference as r

def metrics(ps,ys):
    bins=[]
    for b in range(10):
        ids=[i for i,p in enumerate(ps) if min(9,int(p*10))==b]
        if ids: bins.append({'n':len(ids),'pred':statistics.mean(ps[i] for i in ids),'actual':statistics.mean(ys[i] for i in ids)})
    return {'brier':statistics.mean((p-y)**2 for p,y in zip(ps,ys)),'ece':sum(x['n']*abs(x['pred']-x['actual']) for x in bins)/len(ys),'bins':bins}

def run():
    results=[]
    # Fixed before evaluating any v0.2 scores; no tuning against these test sets.
    for seed in (31001,31002,31003,31004,31005,31006):
        rng=random.Random(seed)
        pool=[{'code':f'{y}-{i}','label':y,'values':r.ind._base_values(rng,y==0)} for y in (0,1) for i in range(20)]
        test_rng=random.Random(seed+100000)
        test=[{'label':i%2,'values':r.ind._base_values(test_rng,i%2==0)} for i in range(120)]
        train=[];cal=[];split=random.Random(20261027)
        for y in (0,1):
            g=[s for s in pool if s['label']==y];split.shuffle(g);cal+=g[:5];train+=g[5:]
        start=time.perf_counter();models,oob=r._forest(train,60,20261027)
        parameters=r.sigmoid_fit([r._predict(models,s['values']) for s in cal],[s['label'] for s in cal]); fit_seconds=time.perf_counter()-start
        raw=[r._predict(models,s['values']) for s in test]
        calibrated=[r.sigmoid_predict(parameters,p) for p in raw]
        ys=[s['label'] for s in test]
        gate=r.baseline.quality_gate(pool)
        X=[[s['values'][k] for k in r.ind.KEYS]+r.baseline.benford_factors(s['values'],gate) for s in pool]
        brng=random.Random(20261027);trees=[]
        for _ in range(60):
            idx=[brng.randrange(len(pool)) for _ in pool]
            if len({pool[i]['label'] for i in idx})>1:trees.append(r.baseline._Tree(4).fit([X[i] for i in idx],[pool[i]['label'] for i in idx]))
        old=[]
        for s in test:
            x=[s['values'][k] for k in r.ind.KEYS]+r.baseline.benford_factors(s['values'],gate)
            old.append(sum(t.predict(x) for t in trees)/len(trees))
        results.append({'seed':seed,'test_n':len(test),'baseline':metrics(old,ys),'same_forest_raw':metrics(raw,ys),'same_forest_calibrated':metrics(calibrated,ys),'fit_seconds':fit_seconds,'parameters':parameters})
    delta=[x['same_forest_calibrated']['brier']-x['same_forest_raw']['brier'] for x in results]
    out={'version':r.VERSION,'synthetic_only':True,'replicates':results,'calibration_brier_deltas':delta,'calibration_mean_delta':statistics.mean(delta),'calibration_wins':sum(x<0 for x in delta),'selection':'no selection rule or parameter tuned on these test seeds'}
    pool=r.ind.build_sample_pool();out['D']={k:r.deada(pool,v) for k,v in r.ind.TARGET_SCENARIOS.items()}
    Path(__file__).with_name('results_v02.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'mean_delta':out['calibration_mean_delta'],'wins':out['calibration_wins'],'per_seed':[{ 'seed':x['seed'],**{k:round(x[k]['brier'],6) for k in ('baseline','same_forest_raw','same_forest_calibrated')}} for x in results],'D':{k:{'D':v['D'],'degraded':v['降级'],'diagnostics':v.get('solver_diagnostics')} for k,v in out['D'].items()}},ensure_ascii=False,indent=2))
if __name__=='__main__':run()
