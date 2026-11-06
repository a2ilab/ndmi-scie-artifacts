"""
법령 RAG 검색 성능 평가 - Ablation Study
- (A) baseline: 조/항 단위 청크 FAISS 직접 검색
- (B) 요약본 검색: LLM 생성 요약본 FAISS 검색
- (C) 요약본 + 용어 확장: 요약본 검색에 용어 확장 사전 적용

평가 지표: Recall@5, Recall@10, Recall@20
평가 데이터: AI Hub QA 200건 (eval_dataset_200.json)
"""

import json
from paths import path_setting, output_path
import re
import time
import torch
from collections import OrderedDict
from datetime import datetime
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

# =================================================================
# 설정
# =================================================================
MODEL_NAME = "nlpai-lab/KURE-v1"

INDEX_PATHS = {"A_baseline": path_setting("SCIE_BASELINE_INDEX", "local_inputs/faiss_db_aug_merged"), "B_summary": path_setting("SCIE_SUMMARY_INDEX_V2", "local_inputs/faiss_law_summary_index_v2")}

TERM_DICT_PATH = path_setting("SCIE_TERM_DICT", "data/terms/term_expansion_dict.json")
EVAL_DATA_PATH = path_setting("SCIE_EVAL_DATA", "data/evaluation/eval_dataset_200.json")
OUTPUT_FILE = output_path(f"ablation_study_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

SEARCH_K = 100       # 내부 검색 시 넉넉히 가져올 개수
EVAL_K_LIST = [5, 10, 20]  # 평가할 K값 리스트

# =================================================================
# 유틸리티 함수
# =================================================================
def normalize_law_name(name: str) -> str:
    """법령명 정규화"""
    name = name.strip()
    name = name.replace("·", "ㆍ").replace("•", "ㆍ")
    name = re.sub(r"\s+", " ", name)
    return name

def strip_enforcement_suffix(name: str) -> str:
    """시행령/시행규칙 → 모법명으로 통합"""
    name = name.strip()
    suffixes = [" 시행규칙", " 시행령", " 규정"]
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[:-len(suffix)].strip()
    return name

def normalize_for_matching(name: str) -> str:
    """매칭용 정규화: 모법으로 통합 + 일반 정규화"""
    return normalize_law_name(strip_enforcement_suffix(name))

def deduplicate_by_law(results_with_scores: list) -> list:
    """
    검색 결과를 법령명 기준으로 중복 제거.
    동일 법령이 여러 번 나오면 최고 유사도만 남김.
    """
    law_best = OrderedDict()

    for doc, l2_distance in results_with_scores:
        cosine_sim = 1.0 - (l2_distance ** 2) / 2.0
        cosine_sim = max(0.0, min(1.0, cosine_sim))

        # 메타데이터에서 법령명 추출 (인덱스마다 키가 다를 수 있음)
        raw_law_name = (
            doc.metadata.get("법령명") or
            doc.metadata.get("law_name") or
            doc.metadata.get("법령") or
            "Unknown"
        )

        norm_key = normalize_for_matching(raw_law_name)

        record = {
            "raw_law_name": raw_law_name,
            "normalized_law_name": norm_key,
            "cosine_similarity": cosine_sim,
            "content_preview": doc.page_content[:100],
        }

        if norm_key not in law_best:
            law_best[norm_key] = record
        else:
            if cosine_sim > law_best[norm_key]["cosine_similarity"]:
                law_best[norm_key] = record

    sorted_laws = sorted(law_best.values(), key=lambda x: x["cosine_similarity"], reverse=True)
    return sorted_laws

def check_law_hit(target_law: str, deduped_laws: list) -> bool:
    """정답 법령이 중복제거된 검색 결과에 있는지 확인"""
    norm_target = normalize_for_matching(target_law)
    for law_rec in deduped_laws:
        norm_retrieved = law_rec["normalized_law_name"]
        if norm_target == norm_retrieved:
            return True
        if norm_target in norm_retrieved or norm_retrieved in norm_target:
            return True
    return False

def expand_query(query: str, term_dict: dict) -> str:
    """
    용어 확장 사전을 사용하여 쿼리에 관련 법률 용어를 추가.
    원본 쿼리는 유지하고 매칭된 확장 용어를 뒤에 추가.
    """
    expansion_terms = set()
    
    for term, expansions in term_dict.items():
        if term in query:
            for exp in expansions:
                expansion_terms.add(exp)
    
    if expansion_terms:
        expanded = query + " " + " ".join(expansion_terms)
        return expanded
    return query

def calculate_recall_at_k(true_laws: list, deduped_laws: list, k: int) -> float:
    """Recall@K 계산: 상위 K개 법령 중 정답 포함 비율"""
    if not true_laws:
        return 1.0
    top_k_laws = deduped_laws[:k]
    hit_count = sum(1 for law in true_laws if check_law_hit(law, top_k_laws))
    return hit_count / len(true_laws)

# =================================================================
# 메인 실행
# =================================================================
def main():
    print(f"[{time.strftime('%H:%M:%S')}] 임베딩 모델 로드 중...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    embeddings = HuggingFaceEmbeddings(
        model_name=MODEL_NAME,
        model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": True},
    )
    print(f"[{time.strftime('%H:%M:%S')}] 임베딩 모델 로드 완료 (device: {device})")

    # 평가 데이터 로드
    print(f"[{time.strftime('%H:%M:%S')}] 평가 데이터 로드 중...")
    with open(EVAL_DATA_PATH, 'r', encoding='utf-8') as f:
        eval_data = json.load(f)
    print(f"[{time.strftime('%H:%M:%S')}] 평가 데이터 로드 완료: {len(eval_data)}건")

    # 용어 확장 사전 로드
    print(f"[{time.strftime('%H:%M:%S')}] 용어 확장 사전 로드 중...")
    with open(TERM_DICT_PATH, 'r', encoding='utf-8') as f:
        term_data = json.load(f)
    term_dict = term_data.get("expansion", term_data)
    print(f"[{time.strftime('%H:%M:%S')}] 용어 확장 사전 로드 완료: {len(term_dict)}개 용어")

    # =================================================================
    # 전략별 평가 수행
    # =================================================================
    all_results = {}

    strategies = [
        ("A_baseline", "A_baseline", False),   # (전략명, 인덱스키, 용어확장여부)
        ("B_summary", "B_summary", False),
        ("C_summary_termexp", "B_summary", True),
    ]

    for strategy_name, index_key, use_term_expansion in strategies:
        index_path = INDEX_PATHS[index_key]
        print(f"\n{'='*70}")
        print(f"전략 [{strategy_name}] 평가 시작")
        print(f"  인덱스: {index_path}")
        print(f"  용어 확장: {'O' if use_term_expansion else 'X'}")
        print(f"{'='*70}")

        # FAISS 인덱스 로드
        print(f"  인덱스 로드 중...")
        t0 = time.time()
        vectorstore = FAISS.load_local(
            index_path, embeddings, allow_dangerous_deserialization=True
        )
        print(f"  인덱스 로드 완료 ({time.time()-t0:.1f}s)")

        case_results = []

        for i, qa in enumerate(eval_data):
            query = qa["question"]
            true_laws = qa["matched_laws"]

            # 용어 확장 적용
            if use_term_expansion:
                search_query = expand_query(query, term_dict)
            else:
                search_query = query

            # FAISS 검색
            t0 = time.time()
            raw_results = vectorstore.similarity_search_with_score(search_query, k=SEARCH_K)
            search_time = time.time() - t0

            # 법령 중복 제거
            deduped_laws = deduplicate_by_law(raw_results)

            # 각 K값에 대해 Recall 계산
            recalls = {}
            for k in EVAL_K_LIST:
                recalls[k] = calculate_recall_at_k(true_laws, deduped_laws, k)

            case_result = {
                "id": qa["id"],
                "question": query,
                "true_laws": true_laws,
                "recalls": recalls,
                "search_time": search_time,
                "expanded_query": search_query if use_term_expansion and search_query != query else None,
                "deduped_count": len(deduped_laws),
                "top5_laws": [r["normalized_law_name"] for r in deduped_laws[:5]],
            }
            case_results.append(case_result)

            # 진행 로그 (50건마다)
            if (i + 1) % 50 == 0:
                avg_r5 = sum(r["recalls"][5] for r in case_results) / len(case_results)
                print(f"  [{i+1}/{len(eval_data)}] 현재 평균 Recall@5: {avg_r5:.4f}")

        all_results[strategy_name] = case_results

        # 전략별 요약
        n = len(case_results)
        print(f"\n  [{strategy_name}] 결과 요약:")
        for k in EVAL_K_LIST:
            avg_recall = sum(r["recalls"][k] for r in case_results) / n
            print(f"    Recall@{k}: {avg_recall:.4f}")

        # 메모리 해제
        del vectorstore
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # =================================================================
    # 결과 저장
    # =================================================================
    import os
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("법령 RAG Ablation Study 결과\n")
        f.write(f"평가 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"임베딩 모델: {MODEL_NAME}\n")
        f.write(f"평가 데이터: {len(eval_data)}건\n")
        f.write(f"검색 K: {SEARCH_K} → 법령 중복제거 후 상위 K개 평가\n")
        f.write("=" * 80 + "\n\n")

        # 전체 요약 표
        f.write("[ 전체 요약 ]\n")
        f.write("-" * 60 + "\n")
        header = f"{'전략':<30}"
        for k in EVAL_K_LIST:
            header += f" | {'Recall@'+str(k):>10}"
        f.write(header + "\n")
        f.write("-" * 60 + "\n")

        for strategy_name in all_results:
            results = all_results[strategy_name]
            n = len(results)
            line = f"{strategy_name:<30}"
            for k in EVAL_K_LIST:
                avg = sum(r["recalls"][k] for r in results) / n
                line += f" | {avg:>10.4f}"
            f.write(line + "\n")
        f.write("-" * 60 + "\n\n")

        # 전략 간 비교 (B vs A, C vs A)
        f.write("[ baseline 대비 향상 (%p) ]\n")
        f.write("-" * 60 + "\n")
        baseline_results = all_results["A_baseline"]
        n = len(baseline_results)
        baseline_recalls = {}
        for k in EVAL_K_LIST:
            baseline_recalls[k] = sum(r["recalls"][k] for r in baseline_results) / n

        for strategy_name in ["B_summary", "C_summary_termexp"]:
            results = all_results[strategy_name]
            line = f"{strategy_name:<30}"
            for k in EVAL_K_LIST:
                avg = sum(r["recalls"][k] for r in results) / n
                diff = avg - baseline_recalls[k]
                line += f" | {'+' if diff >= 0 else ''}{diff*100:>8.2f}%p"
            f.write(line + "\n")
        f.write("-" * 60 + "\n\n")

        # 사례별 상세 결과
        f.write("[ 사례별 상세 결과 ]\n")
        f.write("=" * 80 + "\n\n")

        for i, qa in enumerate(eval_data):
            f.write(f"--- {qa['id']} ---\n")
            f.write(f"질의: {qa['question'][:100]}...\n")
            f.write(f"정답 법령: {qa['matched_laws']}\n")

            for strategy_name in all_results:
                r = all_results[strategy_name][i]
                recalls_str = ", ".join([f"R@{k}={r['recalls'][k]:.2f}" for k in EVAL_K_LIST])
                f.write(f"  [{strategy_name}] {recalls_str}")
                if r.get("expanded_query"):
                    f.write(f" | 확장쿼리: {r['expanded_query'][:80]}...")
                f.write(f"\n")
                f.write(f"    Top5: {r['top5_laws']}\n")
            f.write("\n")

    print(f"\n{'='*70}")
    print(f"평가 완료! 결과 저장: {OUTPUT_FILE}")
    print(f"{'='*70}")

    # 최종 요약 출력
    print(f"\n[ 최종 결과 요약 ]")
    print(f"{'-'*60}")
    header = f"{'전략':<30}"
    for k in EVAL_K_LIST:
        header += f" | {'Recall@'+str(k):>10}"
    print(header)
    print(f"{'-'*60}")
    for strategy_name in all_results:
        results = all_results[strategy_name]
        n = len(results)
        line = f"{strategy_name:<30}"
        for k in EVAL_K_LIST:
            avg = sum(r["recalls"][k] for r in results) / n
            line += f" | {avg:>10.4f}"
        print(line)
    print(f"{'-'*60}")

if __name__ == "__main__":
    main()