"""Public-data-only portability checks; no private pattern/cache required."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'revision/code'))
import revision_adapter as adapter

class PortableTests(unittest.TestCase):
    @unittest.skipUnless((ROOT/'data/metadata/population.csv').is_file(),
                         'Population inputs are external in the code-only export')
    def test_official_denominator_variants_match_supplied_reference_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'out'
            result=adapter.run_population(ROOT,output,denominators_only=True)
            self.assertEqual(result['status'],'PASS')
            for name in ('denominator_variants_0_74.csv','denominator_cell_differences_0_74.csv'):
                self.assertEqual((output/'preinference'/name).read_bytes(),
                                 (ROOT/'revision/results'/('Revision_population_'+name)).read_bytes())

    def test_manifest_mismatch_rejected(self):
        original=json.loads((ROOT/'input_manifest.json').read_text(encoding='utf-8'))
        original['files']['residence_audit_source']['sha256']='0'*64
        with tempfile.TemporaryDirectory() as temp:
            manifest=Path(temp)/'input_manifest.json'
            manifest.write_text(json.dumps(original),encoding='utf-8')
            with self.assertRaisesRegex(adapter.AdapterError,'sha256 mismatch'):
                adapter.verify_inputs(ROOT,['residence_audit_source'],manifest)

    def test_external_pattern_not_silently_substituted(self):
        with self.assertRaises(adapter.AdapterError):
            adapter.verify_inputs(ROOT,['population_pattern_file'])

    def test_output_cannot_overwrite_input_tree(self):
        with self.assertRaises(adapter.AdapterError):
            adapter._check_output_dir(ROOT,ROOT)

if __name__=='__main__':
    unittest.main()
