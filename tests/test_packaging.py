"""Small regressions for provenance overlap/direction and output isolation."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'src')]
from export_provenance import build_provenance, term_row, TERM_COLUMNS
from paths import path_setting, output_path


class PackagingTests(unittest.TestCase):
    def test_overlap_reverse_direction_and_daily_pairs(self):
        api = {'일상A': [{'법령용어': '법률말', '관계': '상위어'}],
               '반대': [{'법령용어': '법률말', '관계': '반의어'}]}
        summaries = [{'일상용어_동의어': ['법률말=일상A、일상B']}]
        edges = build_provenance(api, summaries)
        records = {pair: dict(zip(TERM_COLUMNS, term_row(pair, mask))) for pair, mask in edges.items()}
        self.assertEqual(len(records), 6)
        forward = records[('일상A', '법률말')]
        self.assertEqual(forward['source'], 'API|LLM')
        self.assertEqual(json.loads(forward['api_relation_forward']), ['상위어'])
        self.assertEqual(json.loads(forward['api_relation_reverse']), [])
        reverse = records[('법률말', '일상A')]
        self.assertEqual(json.loads(reverse['api_relation_reverse']), ['상위어'])
        self.assertEqual(json.loads(reverse['api_relation_forward']), [])
        self.assertEqual(records[('일상A', '일상B')]['source'], 'LLM')
        self.assertEqual(json.loads(records[('일상A', '일상B')]['llm_mapping_kind']), ['daily_to_daily'])
        self.assertFalse(any('반대' in pair for pair in records))

    def test_duplicate_relations_preserve_both_origins(self):
        api = {'a': [{'법령용어': 'b', '관계': '동의어'}, {'법령용어': 'b', '관계': '연관어'}]}
        row = dict(zip(TERM_COLUMNS, term_row(('a', 'b'), build_provenance(api, [])[('a', 'b')])))
        self.assertEqual(row['source'], 'API')
        self.assertEqual(json.loads(row['api_relation_forward']), ['동의어', '연관어'])

    def test_unknown_api_shape_fails(self):
        with self.assertRaises(ValueError):
            build_provenance({'a': {'unexpected': 'b'}}, [])

    def test_relative_inputs_resolve_from_repository(self):
        with patch.dict(os.environ, {'SCIE_TEST_INPUT': 'data/corpus/law_catalog.json'}):
            self.assertEqual(path_setting('SCIE_TEST_INPUT', 'unused'), str(ROOT / 'data/corpus/law_catalog.json'))

    def test_outputs_cannot_overwrite_preserved_data(self):
        with patch.dict(os.environ, {'SCIE_OUTPUT_DIR': str(ROOT / 'data')}):
            with self.assertRaises(ValueError):
                output_path('summaries/law_summaries_all.json')
        with patch.dict(os.environ, {'SCIE_OUTPUT_DIR': str(ROOT / 'outputs')}):
            with self.assertRaises(ValueError):
                output_path('../data/corpus/law_catalog.json')


if __name__ == '__main__':
    unittest.main()
