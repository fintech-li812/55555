"""Offline policy candidates. No services, database access or external actions."""
import math, random
from datetime import date

LEVELS={'绿灯':0,'黄灯':1,'红灯':2}


def quantile(values,q):
    a=sorted(values);p=(len(a)-1)*q;i=int(p)
    return a[i]+(a[min(i+1,len(a)-1)]-a[i])*(p-i)


def threshold_policy(history,static_yellow,static_red,as_of,*,window=12,bootstrap=199,seed=20261029):
    if type(window) is not int or window<12 or type(bootstrap) is not int or bootstrap<99:
        raise ValueError('window >=12 and bootstrap >=99 required')
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (static_red,static_yellow)) or static_red>=static_yellow:
        raise ValueError('require 0 <= static_red < static_yellow')
    result={'enabled':False,'mode':'static','yellow':static_yellow,'red':static_red,'confidence':None,'window':window,'policy':'static_floor','version':'threshold-policy-0.1'}
    def fallback(reason):return dict(result,reason=reason)
    if not isinstance(history,list):return fallback('history must be a monthly list')
    try:
        cutoff=date.fromisoformat(as_of+'-01')
        rows=[]
        for item in history:
            month=item['month'];d=date.fromisoformat(month+'-01');v=item['ratio']
            if month!=d.strftime('%Y-%m') or type(v) not in (int,float) or not math.isfinite(v) or v<0:
                return fallback('invalid month or ratio; no silent row deletion')
            rows.append((d,v))
    except (ValueError,TypeError,KeyError):return fallback('invalid monthly history or as_of')
    if len({d for d,v in rows})!=len(rows):return fallback('duplicate month')
    if any(d>=cutoff for d,v in rows):return fallback('current or future month rejected to prevent lookahead')
    rows.sort()
    if len(rows)<window:return fallback('insufficient monthly history')
    rows=rows[-window:]
    indexes=[d.year*12+d.month for d,v in rows]
    if indexes[-1]!=cutoff.year*12+cutoff.month-1 or any(b-a!=1 for a,b in zip(indexes,indexes[1:])):
        return fallback('history must be consecutive and end in previous month')
    values=[v for d,v in rows];y=quantile(values,.25);r=quantile(values,.10)
    if y<=r:return fallback('degenerate quantiles')
    rng=random.Random(seed);draws=[]
    for _ in range(bootstrap):
        sample=[]
        while len(sample)<window:
            start=rng.randrange(window);sample.extend(values[(start+j)%window] for j in range(3))
        sample=sample[:window];draws.append((quantile(sample,.10),quantile(sample,.25)))
    intervals={k:[quantile([d[i] for d in draws],.025),quantile([d[i] for d in draws],.975)] for i,k in enumerate(('red','yellow'))}
    return dict(result,enabled=True,mode='dynamic_with_static_floor',yellow=max(static_yellow,y),red=max(static_red,r),raw_quantiles={'yellow':y,'red':r},confidence={'type':'exploratory circular 3-month block bootstrap percentile interval','intervals':intervals,'coverage_guaranteed':False,'bootstrap':bootstrap},reason='consecutive lagged history passed; static floor prevents normalization of chronic distress')


def unified_view(cross_section,buffer_level,temporal_probability,*,temporal_validated=False,yellow_probability=.1,red_probability=.3):
    if not 0<=yellow_probability<red_probability<=1:raise ValueError('invalid probability thresholds')
    components={};missing=[]
    for name,level in (('cross_section',cross_section),('buffer',buffer_level)):
        if level in LEVELS:components[name]=level
        else:missing.append(name)
    valid=type(temporal_probability) in (int,float) and math.isfinite(temporal_probability) and 0<=temporal_probability<=1
    if valid and temporal_validated:
        components['temporal']='红灯' if temporal_probability>=red_probability else '黄灯' if temporal_probability>=yellow_probability else '绿灯'
    else:missing.append('temporal')
    if not components:return {'level':'待复核','complete':False,'missing':missing,'components':components,'policy_version':'risk-view-0.1'}
    level=max(components.values(),key=LEVELS.get)
    if missing and level=='绿灯':level='黄灯'
    return {'level':level,'complete':not missing,'missing':missing,'components':components,'policy_version':'risk-view-0.1','reason':'maximum component severity; incomplete evidence cannot support green','probability_combined':False,'business_approval':'candidate policy, not deployed'}
