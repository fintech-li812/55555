"""Frozen distribution-shift check; train once, no tuning against evaluation."""
import random,json,statistics
from pathlib import Path
import reference as r
from experiment_v02 import metrics

def run():
 rng=random.Random(41001)
 pool=[{'code':str(i)+'-'+str(y),'label':y,'values':r.ind._base_values(rng,y==0)} for y in (0,1) for i in range(60)]
 groups=[[s for s in pool if s['label']==y] for y in (0,1)]
 split=random.Random(20261027);train=[];cal=[]
 for g in groups:
  split.shuffle(g);cal+=g[:15];train+=g[15:]
 models,oob=r._forest(train,60,20261027)
 parameters=r.sigmoid_fit([r._predict(models,s['values']) for s in cal],[s['label'] for s in cal])
 results={}
 for prevalence in (.1,.5,.9):
  test_rng=random.Random(51000+int(prevalence*10));ys=[1]*int(200*prevalence)+[0]*(200-int(200*prevalence))
  values=[r.ind._base_values(test_rng,y==0) for y in ys]
  raw=[r._predict(models,v) for v in values];ps=[r.sigmoid_predict(parameters,p) for p in raw]
  results[str(prevalence)]={'n':200,'raw':metrics(raw,ys),'calibrated':metrics(ps,ys)}
 result={'training_n':120,'calibration_n':30,'training_risk_prevalence':.5,'calibration_diagnostics':r.sigmoid_diagnostics(parameters,[r._predict(models,s['values']) for s in cal],[s['label'] for s in cal]),'tests':results,'caveat':'label prevalence shift only, shared synthetic conditional distribution; no correction fitted on test labels'}
 Path(__file__).with_name('calibration_shift_v03.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({k:{m:{metric:round(v[m][metric],6) for metric in ('brier','ece')} for m in ('raw','calibrated')} for k,v in results.items()},indent=2))
if __name__=='__main__':run()
