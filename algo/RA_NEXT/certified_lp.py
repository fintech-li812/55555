"""Independent two-phase Bland simplex for standard-form LP, stdlib only.
Maximize c*x subject to A*x=b, x>=0. Certificate is numerically checked.
"""
import math


def linear_solve(A,b,tol=1e-12):
    n=len(b); t=[list(row)+[rhs] for row,rhs in zip(A,b)]
    for j in range(n):
        i=max(range(j,n),key=lambda i:abs(t[i][j]))
        if abs(t[i][j])<tol: return None
        t[j],t[i]=t[i],t[j]; pivot=t[j][j]
        t[j]=[v/pivot for v in t[j]]
        for i in range(n):
            if i!=j:
                f=t[i][j];t[i]=[v-f*w for v,w in zip(t[i],t[j])]
    return [row[-1] for row in t]


def certificate(c,A,b,x,y,tol=1e-7):
    if len(x)!=len(c) or len(y)!=len(b) or any(not math.isfinite(v) for v in x+y):
        return {'optimality_verified':False,'reason':'invalid certificate shape or values'}
    primal=max((abs(sum(a*v for a,v in zip(row,x))-rhs)/(1+abs(rhs)+sum(abs(a*v) for a,v in zip(row,x))) for row,rhs in zip(A,b)),default=0)
    dual_slack=[sum(row[j]*yi for row,yi in zip(A,y))-cj for j,cj in enumerate(c)]
    dual=max((max(0,-s)/(1+abs(c[j])+sum(abs(row[j]*yi) for row,yi in zip(A,y))) for j,s in enumerate(dual_slack)),default=0)
    po=sum(ci*xi for ci,xi in zip(c,x));do=sum(bi*yi for bi,yi in zip(b,y))
    gap=abs(po-do)/(1+abs(po)+abs(do));negative=max(0,-min(x,default=0))
    return {'optimality_verified':max(primal,dual,gap,negative)<=tol,'relative_primal_residual':primal,'relative_dual_violation':dual,'relative_duality_gap':gap,'negative_variable':negative,'primal_objective':po,'dual_objective':do,'tolerance':tol,'reason':'numerical primal-dual certificate; not exact arithmetic proof'}


def solve(c,A,b,max_iter=10000):
    n=len(c);m=len(b)
    if not n or not m or len(A)!=m or any(len(row)!=n for row in A) or any(type(v) not in (int,float) or not math.isfinite(v) for row in [c,b]+list(A) for v in row):
        return None,None,False,{'reason':'invalid LP dimensions or coefficients','optimality_verified':False}
    signs=[1 if v>=0 else -1 for v in b]
    aa=[[v*signs[i] for v in row] for i,row in enumerate(A)]
    t=[row+[float(i==j) for j in range(m)]+[b[i]*signs[i]] for i,row in enumerate(aa)]
    basis=[n+i for i in range(m)];active=list(range(m));pivots=0
    def pivot(i,j):
        nonlocal pivots
        v=t[i][j];t[i]=[x/v for x in t[i]]
        for k in range(len(t)):
            if k!=i:
                f=t[k][j]
                if abs(f)>1e-15:t[k]=[x-f*y for x,y in zip(t[k],t[i])]
        basis[i]=j;pivots+=1
    def optimize(cost,columns):
        for _ in range(max_iter):
            reduced=[cost[j]-sum(cost[basis[i]]*t[i][j] for i in range(len(t))) for j in range(columns)]
            enter=next((j for j,v in enumerate(reduced) if v>1e-9 and j not in basis),None)
            if enter is None:return 'optimal'
            choices=[i for i in range(len(t)) if t[i][enter]>1e-9]
            if not choices:return 'unbounded'
            ratio=min(t[i][-1]/t[i][enter] for i in choices)
            leave=min((i for i in choices if abs(t[i][-1]/t[i][enter]-ratio)<=1e-9),key=lambda i:basis[i])
            pivot(leave,enter)
        return 'iteration_limit'
    phase=optimize([0]*n+[-1]*m,n+m)
    artificial=sum(t[i][-1] for i in range(len(t)) if basis[i]>=n)
    if phase!='optimal' or artificial>1e-7:
        return None,None,False,{'reason':'phase1_'+phase if phase!='optimal' else 'infeasible','optimality_verified':False,'artificial_sum':artificial}
    for i in range(len(t)-1,-1,-1):
        if basis[i]>=n:
            j=next((j for j in range(n) if j not in basis and abs(t[i][j])>1e-9),None)
            if j is not None:pivot(i,j)
            elif abs(t[i][-1])<=1e-7:
                del t[i];del basis[i];del active[i]
            else:return None,None,False,{'reason':'infeasible_redundant_row','optimality_verified':False}
    # Remaining tableau rows can be arbitrary combinations of original equations.
    # Recover y from ALL original rows via original basis columns using elimination.
    t[:]=[row[:n]+[row[-1]] for row in t]
    phase=optimize(list(c),n)
    if phase!='optimal':return None,None,False,{'reason':phase,'optimality_verified':False}
    x=[0.0]*n
    for i,j in enumerate(basis):x[j]=t[i][-1]
    # Solve B^T y=c_B using original equations. Deleted redundant equation y=0.
    bt=[[aa[i][j] for i in active] for j in basis]
    yy=linear_solve(bt,[c[j] for j in basis])
    if yy is None:return None,None,False,{'reason':'singular_certificate_basis','optimality_verified':False}
    y=[0.0]*m
    for i,v in zip(active,yy):y[i]=v*signs[i]
    info=certificate(c,A,b,x,y);info.update(pivots=pivots,dual=y)
    return x,sum(ci*xi for ci,xi in zip(c,x)),info['optimality_verified'],info
