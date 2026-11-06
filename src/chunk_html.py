"""CLI for the preserved candidate parser; does not build a vector index."""
from pathlib import Path
import argparse
import json
from paths import path_setting, output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=path_setting('SCIE_LAW_HTML', 'local_inputs/law_html'))
    parser.add_argument('--output', default='chunks.jsonl', help='Path relative to SCIE_OUTPUT_DIR')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    source = Path(args.input)
    files = sorted(source.glob('*.html')) if source.is_dir() else [source] if source.is_file() else []
    if not files:
        parser.error('No HTML inputs found')
    if args.limit is not None:
        files = files[:args.limit]
    from legal_parser import LegalParser
    engine = LegalParser()
    count = 0
    target = output_path(args.output)
    with open(target, 'w', encoding='utf-8') as stream:
        for path in files:
            for doc in engine.parse_optimized_legal_chunks(path.read_text(encoding='utf-8', errors='ignore'), path.name):
                stream.write(json.dumps({'page_content': doc.page_content, 'metadata': doc.metadata}, ensure_ascii=False) + '\n')
                count += 1
    print(json.dumps({'files': len(files), 'chunks': count, 'output': target}, ensure_ascii=False))


if __name__ == '__main__':
    main()
