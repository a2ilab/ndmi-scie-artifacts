"""
2단계 파이프라인 통합 비교
============================
방식 1 (S): 요약본 기반 1단계 → 그래프 확장 → 2단계 청크 재검색
방식 2 (B): 기존 인덱스 기반 1단계 (법령명 추출) → 그래프 확장 → 2단계 청크 재검색

비교 전략:
  A:  baseline (조문 청크 직접 검색, 비교 기준)
  S10, S20: 요약본 기반 파이프라인 (1단계 K=10,20)
  B10, B20: 기존인덱스 기반 파이프라인 (1단계 K=50,100 → 법령 추출 ~10,20개)
"""

import json
from paths import path_setting, output_path
import re
import time
import numpy as np
import torch
from collections import OrderedDict
from datetime import datetime
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

# =================================================================
# 설정
# =================================================================
MODEL_NAME = "nlpai-lab/KURE-v1"

BASELINE_INDEX = path_setting("SCIE_BASELINE_INDEX", "local_inputs/faiss_db_aug_merged")
SUMMARY_INDEX = path_setting("SCIE_SUMMARY_INDEX_V3", "local_inputs/faiss_law_summary_index_v3")
GRAPH_PATH = path_setting("SCIE_LAW_GRAPH", "data/graphs/unified_law_graph.json")
TERM_DICT_PATH = path_setting("SCIE_TERM_DICT", "data/terms/term_expansion_dict.json")
EVAL_DATA_PATH = path_setting("SCIE_EVAL_DATA", "data/evaluation/eval_dataset_200.json")
OUTPUT_FILE = output_path(f"pipeline_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

EVAL_K_LIST = [5, 10, 20]
MIN_KEY_LENGTH = 3
MAX_EXPANSIONS = 5
MIN_EXP_LENGTH = 2

# =================================================================
# 유틸리티
# =================================================================
def normalize_law_name(name):
    name = name.strip().replace("·", "ㆍ").replace("•", "ㆍ")
    return re.sub(r"\s+", " ", name)

def strip_enforcement_suffix(name):
    name = name.strip()
    for suffix in [" 시행규칙", " 시행령", " 규정"]:
        if name.endswith(suffix): return name[:-len(suffix)].strip()
    return name

def normalize_for_matching(name):
    return normalize_law_name(strip_enforcement_suffix(name))

def get_law_name(doc):
    return (doc.metadata.get("법령명") or doc.metadata.get("law_name") or
            doc.metadata.get("법령") or "Unknown")

def check_law_hit(target, ranked_laws, k):
    norm_target = normalize_for_matching(target)
    for law_name, score in ranked_laws[:k]:
        if norm_target == law_name or norm_target in law_name or law_name in norm_target:
            return True
    return False

def calculate_recall_at_k(true_laws, ranked_laws, k):
    if not true_laws: return 1.0
    hits = sum(1 for law in true_laws if check_law_hit(law, ranked_laws, k))
    return hits / len(true_laws)

def expand_query_improved(query, term_dict):
    expansion_terms = []
    for term, expansions in term_dict.items():
        if len(term) < MIN_KEY_LENGTH: continue
        if term in query:
            valid = [e for e in expansions if len(e) >= MIN_EXP_LENGTH and e not in query]
            expansion_terms.extend(valid[:MAX_EXPANSIONS])
    if expansion_terms:
        unique = list(dict.fromkeys(expansion_terms))
        return query + " " + " ".join(unique)
    return query

def deduplicate_by_law_from_tuples(results_tuples):
    law_best = OrderedDict()
    for norm_name, sim in results_tuples:
        if norm_name not in law_best or sim > law_best[norm_name]:
            law_best[norm_name] = sim
    return sorted(law_best.items(), key=lambda x: x[1], reverse=True)

# =================================================================
# 그래프 확장
# =================================================================
class GraphExpander:
    def __init__(self, graph_path):
        with open(graph_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.graph = data.get("graph", {})
        print(f"그래프 로드: {len(self.graph)}노드")

    def expand(self, law_names, level_filter=[1, 3]):
        expanded = set()
        for law in law_names:
            for name_variant in [law, strip_enforcement_suffix(law)]:
                if name_variant in self.graph:
                    for related, info in self.graph[name_variant].items():
                        if level_filter and info.get("level", 99) not in level_filter:
                            continue
                        expanded.add(related)
        return expanded

# =================================================================
# 법령별 청크 인덱스 구축
# =================================================================
def build_law_chunk_index(vs_baseline):
    print("baseline 인덱스 법령별 청크 인덱스 구축 중...")
    t0 = time.time()
    law_to_indices = {}
    index_to_id = vs_baseline.index_to_docstore_id
    for faiss_idx, doc_id in index_to_id.items():
        doc = vs_baseline.docstore.search(doc_id)
        if not doc: continue
        norm_name = normalize_for_matching(get_law_name(doc))
        if norm_name not in law_to_indices:
            law_to_indices[norm_name] = []
        law_to_indices[norm_name].append(faiss_idx)
    print(f"  완료: {len(law_to_indices)}개 법령, {len(index_to_id)}개 청크 ({time.time()-t0:.1f}초)")
    return law_to_indices

# =================================================================
# 후보 법령 → 청크 추출 → 유사도 계산 (공통 2단계)
# =================================================================
def stage2_search(candidate_laws_raw, query_embedding, vs_baseline, law_to_indices):
    """
    후보 법령의 모든 청크를 추출하여 유사도 계산.
    candidate_laws_raw: set of 법령명 (원본+정규화 혼합)
    """
    # 후보 법령의 시행령/시행규칙도 추가
    expanded_candidates = set()
    for law in candidate_laws_raw:
        expanded_candidates.add(law)
        expanded_candidates.add(normalize_for_matching(law))
        expanded_candidates.add(strip_enforcement_suffix(law))
        expanded_candidates.add(law + " 시행령")
        expanded_candidates.add(law + " 시행규칙")
        norm = normalize_for_matching(law)
        expanded_candidates.add(norm + " 시행령")
        expanded_candidates.add(norm + " 시행규칙")

    # 후보 법령의 FAISS 인덱스 수집
    candidate_faiss_indices = []
    for norm_name in set(normalize_for_matching(c) for c in expanded_candidates):
        if norm_name in law_to_indices:
            candidate_faiss_indices.extend(law_to_indices[norm_name])
    candidate_faiss_indices = list(set(candidate_faiss_indices))

    if not candidate_faiss_indices:
        return [], 0

    # 벡터 추출 및 유사도 계산
    query_vec = np.array(query_embedding, dtype=np.float32)
    candidate_indices_arr = np.array(candidate_faiss_indices, dtype=np.int64)
    candidate_vectors = vs_baseline.index.reconstruct_batch(candidate_indices_arr)
    similarities = np.dot(candidate_vectors, query_vec)

    # (법령명, 유사도) 생성
    results_tuples = []
    for i, faiss_idx in enumerate(candidate_faiss_indices):
        doc_id = vs_baseline.index_to_docstore_id[faiss_idx]
        doc = vs_baseline.docstore.search(doc_id)
        if doc:
            norm_name = normalize_for_matching(get_law_name(doc))
            results_tuples.append((norm_name, float(similarities[i])))

    ranked = deduplicate_by_law_from_tuples(results_tuples)
    return ranked, len(candidate_faiss_indices)

# =================================================================
# 방식 S: 요약본 기반 1단계
# =================================================================
def pipeline_summary_based(query, vs_summary, vs_baseline, embeddings_model,
                           graph_expander, law_to_indices, stage1_k,
                           term_dict=None, use_term_expand=False):
    search_query = query
    if use_term_expand and term_dict:
        search_query = expand_query_improved(query, term_dict)

    # 1단계: 요약본 검색
    stage1_results = vs_summary.similarity_search_with_score(search_query, k=stage1_k)
    candidate_laws = set()
    for doc, score in stage1_results:
        raw_name = get_law_name(doc)
        candidate_laws.add(raw_name)

    # 그래프 확장
    graph_expanded = graph_expander.expand(candidate_laws)
    candidate_laws.update(graph_expanded)

    # 2단계
    query_embedding = embeddings_model.embed_query(search_query)
    ranked, n_chunks = stage2_search(candidate_laws, query_embedding, vs_baseline, law_to_indices)
    return ranked, len(candidate_laws), n_chunks

# =================================================================
# 방식 B: 기존 인덱스 기반 1단계
# =================================================================
def pipeline_baseline_based(query, vs_baseline, embeddings_model,
                            graph_expander, law_to_indices, stage1_chunk_k,
                            term_dict=None, use_term_expand=False):
    search_query = query
    if use_term_expand and term_dict:
        search_query = expand_query_improved(query, term_dict)

    # 1단계: baseline 인덱스에서 청크 검색 → 법령명 추출
    stage1_results = vs_baseline.similarity_search_with_score(search_query, k=stage1_chunk_k)

    # 법령명 추출 (중복 제거, 등장 순서 유지)
    candidate_laws = set()
    for doc, score in stage1_results:
        raw_name = get_law_name(doc)
        candidate_laws.add(normalize_for_matching(raw_name))

    # 그래프 확장
    graph_expanded = graph_expander.expand(candidate_laws)
    candidate_laws.update(graph_expanded)

    # 2단계
    query_embedding = embeddings_model.embed_query(search_query)
    ranked, n_chunks = stage2_search(candidate_laws, query_embedding, vs_baseline, law_to_indices)
    return ranked, len(candidate_laws), n_chunks

# =================================================================
# 메인
# =================================================================
def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    embeddings = HuggingFaceEmbeddings(
        model_name=MODEL_NAME, model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": True})

    with open(EVAL_DATA_PATH, 'r', encoding='utf-8') as f:
        eval_data = json.load(f)
    with open(TERM_DICT_PATH, 'r', encoding='utf-8') as f:
        term_data = json.load(f)
    term_dict = term_data.get("expansion", term_data)

    print("인덱스 로드 중...")
    vs_baseline = FAISS.load_local(BASELINE_INDEX, embeddings, allow_dangerous_deserialization=True)
    vs_summary = FAISS.load_local(SUMMARY_INDEX, embeddings, allow_dangerous_deserialization=True)
    graph_expander = GraphExpander(GRAPH_PATH)
    law_to_indices = build_law_chunk_index(vs_baseline)

    lines = []
    def log(s=""): lines.append(s); print(s)

    log("=" * 80)
    log("2단계 파이프라인 통합 비교 (요약본 기반 vs 기존인덱스 기반)")
    log(f"평가 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"평가 데이터: {len(eval_data)}건")
    log("=" * 80)

    all_results = {}

    # === A: baseline 직접 검색 ===
    log(f"\n[A_baseline] 평가 중...")
    results_a = []
    for qa in eval_data:
        raw = vs_baseline.similarity_search_with_score(qa["question"], k=100)
        law_best = OrderedDict()
        for doc, l2_dist in raw:
            cosine_sim = max(0.0, min(1.0, 1.0 - (l2_dist ** 2) / 2.0))
            norm_name = normalize_for_matching(get_law_name(doc))
            if norm_name not in law_best or cosine_sim > law_best[norm_name]:
                law_best[norm_name] = cosine_sim
        ranked = sorted(law_best.items(), key=lambda x: x[1], reverse=True)
        recalls = {k: calculate_recall_at_k(qa["matched_laws"], ranked, k) for k in EVAL_K_LIST}
        results_a.append(recalls)
    all_results["A_baseline"] = results_a

    # === 전략 정의 ===
    strategies = [
        # (이름, 방식, 1단계K, 용어확장)
        # 방식 S: 요약본 기반
        ("S10", "summary", 10, False),
        ("S10_te", "summary", 10, True),
        ("S20", "summary", 20, False),
        ("S20_te", "summary", 20, True),
        # 방식 B: 기존인덱스 기반 (청크 K=50→약10개법령, K=100→약20개법령)
        ("B50", "baseline", 50, False),
        ("B50_te", "baseline", 50, True),
        ("B100", "baseline", 100, False),
        ("B100_te", "baseline", 100, True),
        ("B200", "baseline", 200, False),
        ("B200_te", "baseline", 200, True),
    ]

    for name, method, stage1_k, use_te in strategies:
        te_label = "+용어확장" if use_te else ""
        method_label = "요약본" if method == "summary" else "기존인덱스"
        log(f"\n[{name}] 평가 중... ({method_label}, K={stage1_k}{te_label})")

        results = []
        total_cand = 0
        total_chunks = 0

        for i, qa in enumerate(eval_data):
            if method == "summary":
                ranked, n_cand, n_chunks = pipeline_summary_based(
                    qa["question"], vs_summary, vs_baseline, embeddings,
                    graph_expander, law_to_indices, stage1_k,
                    term_dict=term_dict, use_term_expand=use_te)
            else:
                ranked, n_cand, n_chunks = pipeline_baseline_based(
                    qa["question"], vs_baseline, embeddings,
                    graph_expander, law_to_indices, stage1_k,
                    term_dict=term_dict, use_term_expand=use_te)

            recalls = {k: calculate_recall_at_k(qa["matched_laws"], ranked, k) for k in EVAL_K_LIST}
            results.append(recalls)
            total_cand += n_cand
            total_chunks += n_chunks

            if (i + 1) % 50 == 0:
                avg_r10 = sum(r[10] for r in results) / len(results)
                log(f"  [{i+1}/200] R@10={avg_r10:.4f}, "
                    f"평균법령={total_cand/(i+1):.0f}, 평균청크={total_chunks/(i+1):.0f}")

        all_results[name] = results
        avg_c = total_cand / len(eval_data)
        avg_ch = total_chunks / len(eval_data)
        log(f"  완료: 평균 후보법령 {avg_c:.0f}개, 평균 청크 {avg_ch:.0f}개")

    # =========================================================
    # 전체 요약
    # =========================================================
    log(f"\n{'='*80}")
    log("[ 전체 결과 요약 ]")
    log(f"{'─'*80}")
    header = f"{'전략':<20} | {'방식':<12} | {'Recall@5':>10} | {'Recall@10':>10} | {'Recall@20':>10}"
    log(header)
    log(f"{'─'*80}")

    method_map = {"A_baseline": "직접검색"}
    for name, method, k, te in strategies:
        method_map[name] = f"{'요약본' if method == 'summary' else '기존인덱스'} K={k}"

    for name, results in all_results.items():
        n = len(results)
        line = f"{name:<20} | {method_map.get(name, ''):>12}"
        for k in EVAL_K_LIST:
            avg = sum(r[k] for r in results) / n
            line += f" | {avg:>10.4f}"
        log(line)
    log(f"{'─'*80}")

    # baseline 대비 격차
    n = len(eval_data)
    baseline_recalls = {k: sum(r[k] for r in all_results["A_baseline"]) / n for k in EVAL_K_LIST}

    log(f"\n[ baseline 대비 격차 (%p) ]")
    log(f"{'─'*80}")
    for name, results in all_results.items():
        if name == "A_baseline": continue
        line = f"{name:<20}"
        for k in EVAL_K_LIST:
            avg = sum(r[k] for r in results) / n
            diff = avg - baseline_recalls[k]
            line += f" | {'+' if diff>=0 else ''}{diff*100:>8.2f}%p"
        log(line)

    # 방식별 최고 성능 비교
    log(f"\n{'='*80}")
    log("[ 방식별 최고 성능 비교 ]")
    log(f"{'─'*80}")

    best_summary = max(
        [(n, r) for n, r in all_results.items() if n.startswith("S")],
        key=lambda x: sum(r[10] for r in x[1]) / len(x[1]))
    best_baseline = max(
        [(n, r) for n, r in all_results.items() if n.startswith("B")],
        key=lambda x: sum(r[10] for r in x[1]) / len(x[1]))

    log(f"  요약본 기반 최고: {best_summary[0]} (R@10={sum(r[10] for r in best_summary[1])/n:.4f})")
    log(f"  기존인덱스 기반 최고: {best_baseline[0]} (R@10={sum(r[10] for r in best_baseline[1])/n:.4f})")
    log(f"  baseline: A_baseline (R@10={baseline_recalls[10]:.4f})")

    # 저장
    import os
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    log(f"\n결과 저장: {OUTPUT_FILE}")

    del vs_baseline, vs_summary
    if torch.cuda.is_available(): torch.cuda.empty_cache()

if __name__ == "__main__":
    main()