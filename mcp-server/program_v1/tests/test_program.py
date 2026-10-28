from pathlib import Path
import copy,io,json,sys,unittest,zipfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import program_engine as e
from program_service import ProgramService
from agent_guard import validate_result
from service import binding
from dataclasses import replace
sys.path.insert(0,str(ROOT))
import entry
import os
os.environ.update(entry.environment())
from config import Config

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.raw=(ROOT/'inputs'/e.SOURCES['program_demo_v1']).read_bytes()
        self.data=e.read_template(self.raw)
    def calc(self,data=None):return e.calculate_input(self.data if data is None else data,e.sha(self.raw))
    def test_actual_arithmetic(self):
        r=self.calc();self.assertEqual(r['amounts'],dict(opening='100000.00',inflow='130000.00',outflow='90000.00',net='40000.00',closing='140000.00',actual_closing='140000.00',difference='0.00'))
        validate_result(r,binding(r),allow_fixture=True)
    def test_input_changes_result_not_snapshot(self):
        d=dict(self.data,operating_inflow='230000.00',actual_closing='240000.00')
        self.assertEqual(self.calc(d)['amounts']['closing'],'240000.00')
    def test_cent_precision(self):
        d=dict(self.data,operating_inflow='130000.01',actual_closing='140000.01')
        self.assertEqual(self.calc(d)['amounts']['difference'],'0.00')
    def test_missing_each_field(self):
        for key in e.FIELDS:
            with self.subTest(key=key):
                d=dict(self.data);del d[key]
                with self.assertRaises(ValueError):self.calc(d)
    def test_bad_money_not_coerced(self):
        for val in ['NaN','Infinity','1e3','1','1.001',True,123,' 1.00','1,000.00']:
            with self.subTest(val=val),self.assertRaises(ValueError):self.calc(dict(self.data,opening=val))
    def test_no_guessed_units(self):
        with self.assertRaises(ValueError):self.calc(dict(self.data,unit='万元'))
    def test_no_real_data_claim(self):
        with self.assertRaises(ValueError):self.calc(dict(self.data,data_type='REAL_PRIVATE'))
    def test_no_unknown_fields(self):
        with self.assertRaises(ValueError):self.calc(dict(self.data,model_validated='true'))
    def test_independent_balance_required(self):
        with self.assertRaises(ValueError):self.calc(dict(self.data,closing_source=self.data['opening_source']))
    def test_gross_negative_rejected(self):
        with self.assertRaises(ValueError):self.calc(dict(self.data,investing_outflow='-1.00'))
    def test_all_categories_explicit(self):
        for group in ('operating','investing','financing'):
            with self.subTest(group=group),self.assertRaises(ValueError):self.calc(dict(self.data,**{group+'_confirmed':'false'}))
    def test_difference_preserved_blocks_save(self):
        out=e.calculate_source('program_difference_v1')
        self.assertEqual(out['result']['amounts']['difference'],'-1000.00')
        self.assertFalse(out['can_save_source_result'])
        with self.assertRaises(ValueError):ProgramService(Config.from_env()).save(out['result'],'no-write-test')
    def test_negative_closing_flagged(self):
        r=self.calc(dict(self.data,opening='-200000.00',actual_closing='-160000.00'))
        self.assertIn('negative_closing_cash',r['risk_flags'])
    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):e.calculate_source('../../private-file')
    def test_original_sample_not_calculable(self):
        from program_workbench import calculate
        out=calculate('manufacturing_sample_v1')
        self.assertEqual(out['status'],'blocked');self.assertIsNone(out['result'])
        self.assertNotIn('native_recalculation_not_bound',[i['code'] for i in out['readiness']['issues']])
    def test_pinned_hash_enforced(self):
        bad=e.registry();bad['program_demo_v1']['sha256']='0'*64
        with patch.object(e,'registry',return_value=bad),self.assertRaises(ValueError):e.calculate_source()
    def test_source_not_modified(self):
        e.calculate_source();self.assertEqual((ROOT/'inputs'/e.SOURCES['program_demo_v1']).read_bytes(),self.raw)
    def test_result_no_write_claim(self):
        out=e.calculate_source();self.assertFalse(out['database_written']);self.assertTrue(out['fixture'])
        self.assertFalse(out['platform_native_verified']);self.assertEqual(out['result']['origin'],'program_calculation')
    def test_save_tampering_rejected_before_connect(self):
        service=ProgramService(Config.from_env());r=service.read()['result'];r['source']['snapshot_ref']='invented'
        with patch.object(service,'connect',side_effect=AssertionError('must_not_connect')):
            with self.assertRaises(ValueError):service.save(r,'tampered')
    def test_query_historical_context_validation(self):
        service=ProgramService(Config.from_env())
        with self.assertRaises(ValueError):service.query('', '2026-08','x','y')
    def test_false_fixture_label_rejected(self):
        r=self.calc();r['data_type']='REAL_PUBLIC_PROXY'
        with self.assertRaises(ValueError):validate_result(r,binding(r),allow_fixture=True)
    def test_formula_input_rejected(self):
        with zipfile.ZipFile(io.BytesIO(self.raw)) as z:parts={n:z.read(n) for n in z.namelist()}
        parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace(b'<is>',b'<f>1+1</f><is>',1)
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for n,v in parts.items():z.writestr(n,v)
        with self.assertRaises(ValueError):e.read_template(out.getvalue())
    def test_registry_distinct_inputs(self):
        self.assertNotEqual(e.registry()['program_demo_v1']['sha256'],e.registry()['program_difference_v1']['sha256'])
    def test_deterministic_replay(self):self.assertEqual(e.calculate_source(),e.calculate_source())
    def test_decimal_large_without_rounding(self):
        r=self.calc(dict(self.data,opening='9999999999999999.99',actual_closing='10000000000039999.99'))
        self.assertEqual(r['amounts']['difference'],'0.00')
    def test_port_and_qa_environment(self):
        env=entry.environment();self.assertEqual(env['CF_DB_PORT'],'13317');self.assertTrue(env['CF_DB_NAME'].startswith('cfqa_'))

if __name__=='__main__':unittest.main()
