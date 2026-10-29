import unittest
import reference as r
class Tests(unittest.TestCase):
 def test_empty(self):
  self.assertIsNone(r.quality_gate([])['Q'])
 def test_zero_trees(self):
  self.assertIsNone(r.benford_rf(r.ind.build_sample_pool(),{}, {},0)['P'])
 def test_calibration_monotone(self):
  b=r.isotonic_fit([0,.2,.4,.6,1],[0,1,0,1,1]); p=[r.isotonic_predict(b,x/20) for x in range(21)]
  self.assertEqual(p,sorted(p))
 def test_null(self):
  self.assertGreater(r.mc_test([10]*10,[.1]*10,99)['p'],.9)
 def test_missing_D(self):
  self.assertTrue(r.deada([], {})['降级'])
 def test_fusion_grid(self):
  from v8.fusion import fuse
  import itertools
  for q,d,p in itertools.product([None,.1,.4,.8],[None,-1,1],[None,.1,.5,.9]):
   self.assertEqual(r.fuse(q,d,p),fuse(q,d,p))
 def test_repeat(self):
  pool=r.ind.build_sample_pool(); target=r.ind.TARGET_SCENARIOS['edge']; a=r.benford_rf(pool,target,{},10)
  for _ in range(9): self.assertEqual(a,r.benford_rf(pool,target,{},10))


class Iteration02(unittest.TestCase):
 def test_rule_partition(self):
  from v8 import fusion as f
  import itertools
  combos=list(itertools.product(f.ALL_Q,f.ALL_D,f.ALL_P))
  self.assertEqual(len(combos),48)
  for q,d,p in combos:
   self.assertEqual(len([rule for rule in f.RULES if q in rule['q'] and d in rule['d'] and p in rule['p']]),1)
 def test_sigmoid_monotone(self):
  params=r.sigmoid_fit([.1,.2,.5,.7,.9],[0,1,0,1,1])
  ps=[r.sigmoid_predict(params,i/100) for i in range(101)]
  self.assertEqual(ps,sorted(ps))
 def test_invalid_target(self):
  pool=r.ind.build_sample_pool()
  for v in (float('nan'),float('inf'),True,'1',None):
   target=dict(r.ind.TARGET_SCENARIOS['edge']['values']);target['X1']=v
   self.assertTrue(r.deada(pool,target)['降级'])
   self.assertIsNone(r.benford_rf(pool,target,{})['P'])
 def test_invalid_labels(self):
  import copy
  pool=copy.deepcopy(r.ind.build_sample_pool());pool[0]['label']=True
  self.assertTrue(r.deada(pool,r.ind.TARGET_SCENARIOS['edge'])['降级'])
 def test_malformed_pool(self):
  self.assertIsNone(r.quality_gate([{}])['Q'])
  self.assertIsNone(r.deada([{}],{})['D'])
  self.assertIsNone(r.benford_rf([{}],{}, {})['P'])
 def test_solver_lies(self):
  bad=lambda *args:([0.0],0.0,True)
  x,o,ok,info=r.checked_simplex([1],[[1]],[1],bad)
  self.assertFalse(ok)
  self.assertIn('violates',info['reason'])
 def test_infeasible(self):
  x,o,ok,info=r.checked_simplex([1],[[1],[1]],[1,2])
  self.assertFalse(ok)
 def test_known_optimum(self):
  x,o,ok,info=r.checked_simplex([1,0],[[1,1]],[10])
  self.assertTrue(ok);self.assertAlmostEqual(o,10)
 def test_nonfinite_solver(self):
  self.assertFalse(r.checked_simplex([1],[[1]],[1],lambda *a:([float('nan')],0,True))[2])
 def test_invalid_multinomial(self):
  for counts,probs in (([1],[0]),([-1],[1]),([1],[.5]),([1,1],[1])):
   with self.assertRaises(ValueError): r.mc_test(counts,probs)
 def test_degraded_fusion(self):
  for d,p in ((None,.1),(.8,None)):
   self.assertNotEqual(r.fuse(.8,d,p)['等级'],'绿灯')

if __name__=='__main__': unittest.main()
