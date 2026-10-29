from pathlib import Path
p=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT\reference.py')
s=p.read_text(encoding='utf-8-sig')
s=s.replace("blocks=isotonic_fit(scores,[s['label'] for s in calibration])", "blocks=sigmoid_fit(scores,[s['label'] for s in calibration])")
s=s.replace('calibrated=isotonic_predict(blocks,raw)','calibrated=sigmoid_predict(blocks,raw)')
s=s.replace('held-out isotonic; small synthetic sample only','held-out regularized sigmoid; small synthetic sample only')
s+='''

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
'''
p.write_text(s,encoding='utf-8')
e=p.with_name('experiment.py');s=e.read_text(encoding='utf-8-sig').replace('814729','914731');e.write_text(s,encoding='utf-8')
