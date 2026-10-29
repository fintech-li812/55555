import unittest,itertools,random,copy,json
from pathlib import Path
import reference as r
import certified_lp as lp
from decision_policy import threshold_policy,unified_view

class Iteration03(unittest.TestCase):
 def test_certified_known(self):
  x,o,ok,d=lp.solve([3,2,0,0],[[1,1,1,0],[1,0,0,1]],[4,2])
  self.assertTrue(ok);self.assertAlmostEqual(o,10);self.assertTrue(d['optimality_verified'])
 def test_negative_rhs(self):
  self.assertTrue(lp.solve([1],[[-1]],[-2])[2])
 def test_redundant(self):
  self.assertTrue(lp.solve([1],[[1],[2]],[1,2])[2])
 def test_infeasible(self):
  self.assertFalse(lp.solve([1],[[1],[1]],[1,2])[2])
 def test_unbounded(self):
  self.assertFalse(lp.solve([1,0],[[0,1]],[1])[2])
 def test_nonoptimal_feasible_rejected(self):
  self.assertFalse(lp.certificate([1,0],[[1,1]],[10],[0,10],[1])['optimality_verified'])
 def test_invalid_dual(self):
  self.assertFalse(lp.certificate([1,0],[[1,1]],[10],[10,0],[0])['optimality_verified'])
 def test_anchor(self):
  pool=r.ind.build_sample_pool()
  for k,d in [('healthy',.868543),('edge',.187676),('risk',-.471113)]:
   result=r.deada(pool,r.ind.TARGET_SCENARIOS[k]);self.assertEqual(result['D'],d)
   self.assertTrue(result['solver_diagnostics']['baseline_certificate']['optimality_verified'])
 def test_small_calibration_unreliable(self):
  p=r.benford_rf(r.ind.build_sample_pool(),r.ind.TARGET_SCENARIOS['edge'],{})
  self.assertFalse(p['calibration_reliable']);self.assertEqual(p['calibration_n'],10)
 def test_gradient(self):
  params=r.sigmoid_fit([.1,.3,.8,.9],[0,0,1,1])
  self.assertTrue(r.sigmoid_diagnostics(params,[.1,.3,.8,.9],[0,0,1,1])['converged'])
 def test_unconverged_detected(self):
  self.assertFalse(r.sigmoid_diagnostics((99,99),[.1,.9],[0,1])['converged'])
 def test_gate_no_green(self):
  pool=r.ind.build_sample_pool();a=r.assess_reference(pool,r.ind.TARGET_SCENARIOS['healthy'])
  self.assertNotEqual(a['fusion']['等级'],'绿灯')
  a=r.assess_reference(pool,r.ind.TARGET_SCENARIOS['healthy'],benford_applicable=True)
  self.assertNotEqual(a['fusion']['等级'],'绿灯')
 def history(self):return [{'month':f'2025-{i:02d}','ratio':i/3} for i in range(1,13)]
 def policy(self,h):return threshold_policy(h,2.5,1.5,'2026-01')
 def test_threshold_enabled(self):
  a=self.policy(self.history());self.assertTrue(a['enabled']);self.assertGreaterEqual(a['yellow'],2.5);self.assertFalse(a['confidence']['coverage_guaranteed'])
 def test_threshold_insufficient(self):self.assertFalse(self.policy(self.history()[1:])['enabled'])
 def test_threshold_duplicates(self):self.assertFalse(self.policy(self.history()+self.history()[:1])['enabled'])
 def test_threshold_gap(self):
  h=self.history();h[5]['month']='2024-12';self.assertFalse(self.policy(h)['enabled'])
 def test_threshold_future(self):
  h=self.history();h[5]['month']='2026-01';self.assertFalse(self.policy(h)['enabled'])
 def test_threshold_nan(self):
  h=self.history();h[5]['ratio']=float('nan');self.assertFalse(self.policy(h)['enabled'])
 def test_threshold_equal(self):
  h=self.history()
  for x in h:x['ratio']=1
  self.assertFalse(self.policy(h)['enabled'])
 def test_threshold_repeat(self):self.assertEqual(self.policy(self.history()),self.policy(self.history()))
 def test_unified_monotone(self):
  for a,b in itertools.product(('绿灯','黄灯','红灯'),repeat=2):
   out=unified_view(a,b,.3,temporal_validated=True)
   self.assertEqual(out['level'],'红灯')
 def test_unified_missing(self):
  self.assertEqual(unified_view('绿灯','绿灯',None)['level'],'黄灯')
  self.assertEqual(unified_view(None,None,None)['level'],'待复核')
 def test_unified_boundary(self):
  self.assertEqual(unified_view('绿灯','绿灯',.1,temporal_validated=True)['level'],'黄灯')
  self.assertEqual(unified_view('绿灯','绿灯',.3,temporal_validated=True)['level'],'红灯')
 def test_unvalidated_temporal(self):self.assertFalse(unified_view('绿灯','绿灯',.01)['complete'])

if __name__=='__main__':
 import tests_ra_next
 suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromModule(tests_ra_next),unittest.defaultTestLoader.loadTestsFromTestCase(Iteration03)])
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 raise SystemExit(not result.wasSuccessful())
