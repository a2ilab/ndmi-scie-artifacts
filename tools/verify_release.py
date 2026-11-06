"""Offline data/packaging verification, independently using preserved builder logic."""
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
import ast
import contextlib
import csv
import gzip
import hashlib
import io
import json
import os

ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load(relative):
    path = ROOT / relative
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig') as stream:
        return json.load(stream)


def file_hash(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def historical_terms(api, summaries):
    """Only two pure historical transformations; all file I/O replaced in memory."""
    tree = ast.parse((ROOT / 'src/build_graph.py').read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'clean_law_name', 'build_term_expansion_dict'}]
    check(len(functions) == 2, 'Missing preserved transformation functions')
    captured = []
    def memory_load(path):
        if path == 'API':
            return api
        if path == 'LLM':
            return summaries
        raise ValueError(path)
    ns = {'defaultdict': defaultdict, 'datetime': datetime, 'os': os,
          'DAILY_TO_TERM_PATH': 'API', 'SUMMARY_PATH': 'LLM', 'OUTPUT_DIR': '/virtual',
          'load_json': memory_load, 'save_json': lambda result, path: captured.append(result)}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<historical transformations>', 'exec'), ns)
        ns['build_term_expansion_dict']()
    check(len(captured) == 1, 'Historical transformation output missing')
    return captured[0]


def main():
    manifest = load('provenance/source_manifest.json')
    for row in manifest:
        path = ROOT / row['repository_path']
        check(file_hash(path) == row['stored_sha256'], f'Packaged source changed: {path.name}')
        if row['transformation'] == 'byte_copy':
            check(row['stored_sha256'] == row['original_sha256'], f'Original bytes changed: {path.name}')
        elif row['transformation'] == 'gzip_lossless':
            original = gzip.decompress(path.read_bytes())
            check(hashlib.sha256(original).hexdigest() == row['original_sha256'], f'Lossless copy mismatch: {path.name}')
    for folder in ['src', 'tools', 'tests']:
        for path in (ROOT / folder).glob('*.py'):
            compile(path.read_text(), str(path), 'exec')

    catalog = load('data/corpus/law_catalog.json')
    with (ROOT / 'data/corpus/law_ids.csv').open(encoding='utf-8-sig', newline='') as stream:
        check(list(csv.DictReader(stream)) == catalog, 'Law ID export differs from original catalog')
    law_ids = {row['법령MST'] for row in catalog}
    check(len(catalog) == len(law_ids) == 5556, 'Law ID count differs')
    summaries = load('data/summaries/law_summaries_all.json')
    check(len(summaries) == 5556 and {row['법령MST'] for row in summaries} == law_ids, 'Summary scope differs')
    selected = load('data/summaries/dictionary_input_5170.json')
    by_id = {row['법령MST']: row for row in summaries}
    check(len(selected) == len({row['법령MST'] for row in selected}) == 5170, 'Dictionary subset count differs')
    check(all(by_id[row['법령MST']] == row for row in selected), 'Dictionary subset records were edited')
    failed = {row['법령MST'] for row in summaries if row.get('파싱실패')}
    field_missing = {row['법령MST'] for row in summaries if not row.get('파싱실패') and '검색용_요약문' not in row}
    check(failed == {'001674', '005146', '008240', '009206', '014085'}, 'Failure flags differ')
    check(field_missing == {'007709'}, 'Missing-field status differs')
    with (ROOT / 'data/summaries/summary_issues.csv').open(encoding='utf-8-sig', newline='') as stream:
        issue_rows = list(csv.DictReader(stream))
    check(len(issue_rows) == 6 and {row['법령MST'] for row in issue_rows} == failed | field_missing, 'Issue export differs')

    e2 = load('data/evaluation/eval_dataset_200.json')
    e2_ids = (ROOT / 'data/evaluation/e2_ids.txt').read_text().splitlines()
    check(e2_ids == [row['id'] for row in e2] and len(set(e2_ids)) == 200, 'E2 ID export differs')
    tree = ast.parse((ROOT / 'src/generate_summaries.py').read_text())
    strings = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
               and node.targets[0].id in {'SYSTEM_PROMPT', 'USER_PROMPT_TEMPLATE'}}
    for key, name in [('SYSTEM_PROMPT', 'system.txt'), ('USER_PROMPT_TEMPLATE', 'user_template.txt')]:
        check((ROOT / 'prompts' / name).read_text(encoding='utf-8-sig').rstrip('\n') == strings[key].rstrip('\n'), f'Prompt mismatch: {key}')
    check(load('prompts/example_012468.json') == by_id['012468'], 'JSON example differs from stored output')

    api = load('data/terms/api_relations.json.gz')
    saved = load('data/terms/term_expansion_dict.json')
    rebuilt = historical_terms(api, selected)
    for key in ['expansion', 'keyword_to_laws']:
        check(rebuilt[key] == saved[key], f'Preserved builder differs for {key}')
    del rebuilt
    api_only = historical_terms(api, [])['expansion']
    api_edges = {(a, b) for a, targets in api_only.items() for b in targets}
    del api_only
    llm_only = historical_terms({}, selected)['expansion']
    llm_edges = {(a, b) for a, targets in llm_only.items() for b in targets}
    del llm_only
    expected_edges = {(a, b) for a, targets in saved['expansion'].items() for b in targets}
    remaining = expected_edges.copy()
    source_counts = Counter()
    api_forward = defaultdict(set)
    for daily, mappings in api.items():
        for mapping in mappings:
            if mapping.get('법령용어') and mapping.get('관계') in {'동의어', '연관어', '상위어', '하위어'}:
                api_forward[(daily, mapping['법령용어'])].add(mapping['관계'])
    with gzip.open(ROOT / 'data/terms/term_edges.csv.gz', 'rt', encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            edge = (row['source_term'], row['target_term'])
            check(edge in remaining, 'Duplicate or extra exported term edge')
            remaining.remove(edge)
            from_api, from_llm = edge in api_edges, edge in llm_edges
            origin = 'API|LLM' if from_api and from_llm else 'API' if from_api else 'LLM'
            check(row['source'] == origin, 'Term origin mismatch against preserved builder')
            check(row['from_api'] == str(int(from_api)) and row['from_llm'] == str(int(from_llm)), 'Term origin flags differ')
            check(set(json.loads(row['api_relation_forward'])) == api_forward.get(edge, set()), 'Forward API relation labels differ')
            check(set(json.loads(row['api_relation_reverse'])) == api_forward.get((edge[1], edge[0]), set()), 'Reverse API relation labels differ')
            check(bool(json.loads(row['llm_mapping_kind'])) == from_llm, 'LLM mapping provenance missing')
            source_counts[origin] += 1
    check(not remaining, 'Export omitted preserved term edges')
    check(source_counts == {'API': 646469, 'LLM': 98204, 'API|LLM': 5327}, 'Term source counts differ')
    del api_forward, api_edges, llm_edges, expected_edges, remaining

    graph = load('data/graphs/unified_law_graph.json')['graph']
    remaining_graph = {(a, b) for a, targets in graph.items() for b in targets}
    graph_counts = Counter()
    with (ROOT / 'data/graphs/law_edges.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            edge = (row['source_law'], row['target_law'])
            check(edge in remaining_graph, 'Duplicate or extra law edge')
            remaining_graph.remove(edge)
            info = graph[edge[0]][edge[1]]
            check(row['source'] == info['source'] and row['relation'] == info['relation'] and int(row['level']) == info['level'], 'Law graph fields differ')
            check(int(row['from_api']) == int(info['source'] == 'API관련법령'), 'Law API flag differs')
            check(int(row['from_llm']) == int(info['source'] == 'LLM요약'), 'Law LLM flag differs')
            graph_counts[row['source']] += 1
    check(not remaining_graph, 'Export omitted law edges')
    check(graph_counts == {'API관련법령': 744, 'LLM요약': 26336}, 'Law graph counts differ')

    report = {
        'status': 'passed', 'scope': 'offline packaging and data verification only',
        'source_manifest_entries_checked': len(manifest),
        'law_ids': len(law_ids), 'E2_ids': len(e2_ids), 'E1_ids': 'unavailable',
        'summary_records': len(summaries), 'failed_records': len(failed),
        'nonfailure_missing_summary_field_records': len(field_missing),
        'dictionary_input_records': len(selected),
        'term_edge_origins': dict(source_counts), 'law_edge_origins': dict(graph_counts),
        'all_preserved_term_edges_and_keyword_mappings_equal': True,
        'all_exported_origin_flags_and_api_direction_labels_verified': True,
        'model_api_retrieval_experiments_run': False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
