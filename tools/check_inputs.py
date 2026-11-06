"""Read-only input and configuration check; no imports of model libraries."""
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from paths import path_setting


def main():
    inputs = {
        'two_stage_baseline': ('SCIE_BASELINE_INDEX', 'local_inputs/faiss_db_aug_merged'),
        'two_stage_summary_v3': ('SCIE_SUMMARY_INDEX_V3', 'local_inputs/faiss_law_summary_index_v3'),
        'ablation_summary_v2': ('SCIE_SUMMARY_INDEX_V2', 'local_inputs/faiss_law_summary_index_v2'),
        'chunking_html': ('SCIE_LAW_HTML', 'local_inputs/law_html'),
        'generation_fulltext': ('SCIE_FULLTEXT_FILE', 'local_inputs/law_fulltext_all.json'),
        'law_catalog': ('SCIE_LAW_CATALOG', 'data/corpus/law_catalog.json'),
        'evaluation_e2': ('SCIE_EVAL_DATA', 'data/evaluation/eval_dataset_200.json'),
        'term_dictionary': ('SCIE_TERM_DICT', 'data/terms/term_expansion_dict.json'),
        'law_graph': ('SCIE_LAW_GRAPH', 'data/graphs/unified_law_graph.json'),
    }
    results = {}
    for label, (setting, default) in inputs.items():
        path = Path(path_setting(setting, default))
        exists = path.exists()
        if label.startswith(('two_stage_', 'ablation_')):
            exists = all((path / name).is_file() for name in ['index.pkl', 'index.faiss'])
        results[label] = {'available': exists, 'path': str(path)}
    print(json.dumps({'inputs': results, 'api_user_configured': bool(os.environ.get('LAW_API_OC')),
                      'model_or_api_calls': 0}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
