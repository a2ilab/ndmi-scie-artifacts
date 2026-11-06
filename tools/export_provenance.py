"""Export provenance of preserved relations. No API, model, or retrieval calls."""
from pathlib import Path
from collections import Counter, defaultdict
import csv
import gzip
import io
import json

ROOT = Path(__file__).resolve().parents[1]
RELATIONS = ('동의어', '연관어', '상위어', '하위어')
API_MASK = (1 << 8) - 1
LLM_KINDS = ('legal_to_daily', 'daily_to_legal', 'daily_to_daily')
LLM_MASK = sum(1 << (8 + i) for i in range(3))
TERM_COLUMNS = ['source_term', 'target_term', 'source', 'from_api', 'from_llm',
                'api_relation_forward', 'api_relation_reverse', 'llm_mapping_kind']


def load_json(path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def build_provenance(api, summaries):
    """Follow the preserved builder, retaining direction and overlapping origins."""
    edges = {}

    def mark(a, b, mask):
        pair = (a, b)
        edges[pair] = edges.get(pair, 0) | mask

    for daily, mappings in api.items():
        if not isinstance(mappings, list):
            raise ValueError('Unexpected API structure; do not silently drop records')
        for mapping in mappings:
            legal, relation = mapping.get('법령용어', ''), mapping.get('관계', '')
            if not legal or relation not in RELATIONS:
                continue
            bit = RELATIONS.index(relation)
            mark(daily, legal, 1 << bit)
            mark(legal, daily, 1 << (bit + 4))

    for summary in summaries:
        entries = summary.get('일상용어_동의어', [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str) or '=' not in entry:
                continue
            legal, rest = entry.split('=', 1)
            legal = legal.strip()
            daily = [part.strip() for part in rest.replace('·', ',').replace('、', ',').split(',') if part.strip()]
            for word in daily:
                mark(legal, word, 1 << 8)
                mark(word, legal, 1 << 9)
                for other in daily:
                    if word != other:
                        mark(word, other, 1 << 10)
    return edges


def term_row(pair, mask):
    from_api, from_llm = bool(mask & API_MASK), bool(mask & LLM_MASK)
    origin = 'API|LLM' if from_api and from_llm else 'API' if from_api else 'LLM'
    arrays = [
        [relation for i, relation in enumerate(RELATIONS) if mask & (1 << i)],
        [relation for i, relation in enumerate(RELATIONS) if mask & (1 << (i + 4))],
        [kind for i, kind in enumerate(LLM_KINDS) if mask & (1 << (i + 8))],
    ]
    return [*pair, origin, int(from_api), int(from_llm),
            *[json.dumps(values, ensure_ascii=False, separators=(',', ':')) for values in arrays]]


def main():
    api = load_json(ROOT / 'data/terms/api_relations.json.gz')
    summaries = load_json(ROOT / 'data/summaries/dictionary_input_5170.json')
    saved = load_json(ROOT / 'data/terms/term_expansion_dict.json')
    edges = build_provenance(api, summaries)
    saved_count = sum(len(targets) for targets in saved['expansion'].values())
    if len(edges) != saved_count or any((a, b) not in edges for a, targets in saved['expansion'].items() for b in targets):
        raise ValueError('Inputs do not reproduce the preserved expansion edges exactly')
    keywords = defaultdict(set)
    for row in summaries:
        law = str(row.get('법령명', '')).replace(' 시행령', '').replace(' 시행규칙', '').strip()
        for keyword in row.get('규율분야_키워드', []):
            if isinstance(keyword, str) and keyword:
                keywords[keyword].add(law)
    if {k: sorted(v) for k, v in keywords.items()} != saved['keyword_to_laws']:
        raise ValueError('Keyword-to-law mappings differ from the preserved dictionary')

    output = ROOT / 'data/terms/term_edges.csv.gz'
    counts = Counter()
    with output.open('wb') as binary:
        with gzip.GzipFile(fileobj=binary, mode='wb', filename='', mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(TERM_COLUMNS)
                for pair in sorted(edges):
                    row = term_row(pair, edges[pair])
                    counts[row[2]] += 1
                    writer.writerow(row)

    graph = load_json(ROOT / 'data/graphs/unified_law_graph.json')['graph']
    graph_counts = Counter()
    with (ROOT / 'data/graphs/law_edges.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['source_law', 'target_law', 'relation', 'source', 'from_api', 'from_llm', 'level'])
        for source, targets in sorted(graph.items()):
            for target, info in sorted(targets.items()):
                label = info['source']
                if label not in ('API관련법령', 'LLM요약'):
                    raise ValueError(f'Unknown graph origin: {label}')
                writer.writerow([source, target, info['relation'], label,
                                 int(label == 'API관련법령'), int(label == 'LLM요약'), info['level']])
                graph_counts[label] += 1
    report = {
        'term_input_summary_records': len(summaries),
        'term_nodes': len(saved['expansion']),
        'term_directed_edges': len(edges),
        'term_edge_origins': dict(counts),
        'preserved_expansion_edges_exactly_equal': True,
        'preserved_keyword_law_mappings_exactly_equal': True,
        'law_graph_edge_origins': dict(graph_counts),
        'llm_semantic_relation_type': 'unclassified; mapping direction is not a synonym/hypernym label',
    }
    (ROOT / 'provenance/relations.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
