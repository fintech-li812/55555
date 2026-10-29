from pathlib import Path
p=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT\reference.py')
s=p.read_text(encoding='utf-8').replace('RA_NEXT 0.1:', 'RA_NEXT 0.3:').replace("VERSION = 'ra-next-0.2'","VERSION = 'ra-next-0.3'")
s=s.replace("        x,obj,ok,info=checked_simplex(c,A,b)\n        diagnostics.update(info)\n        return x,obj,ok", "        from certified_lp import solve as certified_solve, certificate\n        old_x,old_obj,old_ok,old_info=checked_simplex(c,A,b)\n        x,obj,ok,info=certified_solve(c,A,b)\n        diagnostics.update(info)\n        diagnostics['baseline_objective']=old_obj\n        if old_ok and ok:\n            old_certificate=certificate(c,A,b,old_x,info['dual'])\n            diagnostics['baseline_certificate']=old_certificate\n            if old_certificate['optimality_verified']:\n                return old_x,old_obj,True\n        diagnostics['baseline_preserved']=False\n        return x,obj,ok")
p.write_text(s,encoding='utf-8')
