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
if __name__=='__main__': unittest.main()
