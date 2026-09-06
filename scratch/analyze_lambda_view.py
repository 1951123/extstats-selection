"""208-query PG census per-lambda view: baseline & best-candidate qerror by λ."""
import glob, json, numpy as np
files = sorted(glob.glob('results/measure/census/postgres/query.*.json'))
rows=[]
for f in files:
    d=json.load(open(f)); qid=d['qid']; act=d['actual']
    L0,L1=d['by_lambda']['0'],d['by_lambda']['1']
    b0,b1=L0['baseline']['qerror'],L1['baseline']['qerror']
    best0=min(c['qerror'] for c in L0['candidates']) or float('nan')
    best1=min(c['qerror'] for c in L1['candidates']) or float('nan')
    # lambda_q for L1 = truth*S/N ; estimate capture fidelity
    lam1=(act/2_458_285)*300000 if act else float('nan')
    rows.append(dict(qid=qid,act=act,b0=b0,b1=b1,best0=best0,best1=best1,nc1=len(L1['candidates']),lam1=lam1))
n=len(rows)
print(f'queries={n}')
ba0=np.mean([r['b0'] for r in rows]); ba1=np.mean([r['b1'] for r in rows])
ga0=np.mean([r['best0'] for r in rows if r['best0']==r['best0']])
ga1=np.mean([r['best1'] for r in rows if r['best1']==r['best1']])
print(f'baseline mean  L0={ba0:.2f}  L1={ba1:.2f}')
print(f'best-ext mean  L0={ga0:.2f}  L1={ga1:.2f}')
# how many queries deep-λ helps: best1 < best0 by meaningful margin (>2x) or vice versa
def below(a,b,thr=0.5):  # is a meaningfully < b
    return a is not None and b is not None and a < b and abs(a-b)> (0.5*max(a,b,1e-9))
deep=L1_s=shallow=0
for r in rows:
    if below(r['best1'],r['best0']): deep+=1
    elif below(r['best0'],r['best1']): shallow+=1
    else: L1_s+=1
print(f'-- per-query best λ preference (based on best-ext qerr) --')
print(f'  prefers DEEPER λ L1: {deep} | prefers shallower L0: {shallow} | ~equal: {L1_s}')
# baseline already good at L0 (no ext needed)? track tail needing ext at L1
need0=sum(1 for r in rows if (r['b1'] if r['b1']==r['b1'] else 0)>=5)
need0_shallow=sum(1 for r in rows if (r['best1'] if r['best1']==r['best1'] else 0)>=5)
print(f'  baseline qerr>=5 @L1: {need0}, still>=5 after best ext @L1: {need0_shallow}')
# rank tail (worst L1 baseline)
rows.sort(key=lambda r: -(r['b1'] if r['b1']==r['b1'] else 0))
print('\nTop-10 worst L1-baseline queries (need ext / deep λ):')
for r in rows[:10]:
    print(f"  {r['qid']:8} act={r['act']:6} bl1={r['b1']:8.1f} best1={r['best1'] if r['best1']==r['best1'] else float('nan'):8.2f} lam1={r['lam1'] if r['lam1']==r['lam1'] else float('nan'):6.2f} nc1={r['nc1']}")
# quantify: how many of worst-20 get fixed below 5 at L1 (sparse sufficiency win rate)
import numpy as np
last20=rows[:20]
fixed=sum(1 for r in last20 if r['best1']==r['best1'] and r['best1']<5)
print(f'\nworst-20 by L1 baseline: {fixed}/20 get best-ext qerr<5 @L1')
