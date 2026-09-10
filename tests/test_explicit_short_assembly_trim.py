import unittest
from tests.test_story_r2v_skill import valid_plan, VALIDATOR

class ExplicitShortTrimTests(unittest.TestCase):
    def errors(self, trim):
        plan=valid_plan()
        plan['source_audio']['duration_seconds']=2
        plan['shots'][0]['source_end']=2
        if trim is not None:plan['shots'][0]['assembly_trim']=trim
        return VALIDATOR.validate_plan(plan)

    def test_continuous_short_trim_passes_without_acceleration(self):
        for trim in ({'anchor':'start'},{'anchor':'center'},{'anchor':'end'},{'anchor':'explicit','start_second':8}):
            with self.subTest(trim=trim):self.assertEqual(self.errors(trim),[])

    def test_unbounded_or_unknown_trim_does_not_bypass_retime(self):
        for trim in (None,{'anchor':'unknown'},{'anchor':'explicit','start_second':9},{'anchor':'explicit','start_second':-1}):
            with self.subTest(trim=trim):
                self.assertTrue(any('retime ratio' in error for error in self.errors(trim)))

    def test_high_ratio_remains_rejected(self):
        plan=valid_plan();plan['source_audio']['duration_seconds']=15
        plan['shots'][0].update(source_end=15,assembly_trim={'anchor':'start'})
        self.assertTrue(any('retime ratio' in e for e in VALIDATOR.validate_plan(plan)))
