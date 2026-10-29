from pathlib import Path
p=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT\tests_ra_next.py');s=p.read_text(encoding='utf-8-sig').replace("if __name__=='__main__': unittest.main()",'');s+="\nif __name__=='__main__': unittest.main()\n";p.write_text(s,encoding='utf-8')
