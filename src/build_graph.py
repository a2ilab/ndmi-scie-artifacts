"""
통합 그래프 + 용어 확장 사전 구축
===================================
3개 소스를 결합하여 통합 법령 관계 그래프와 용어 확장 사전을 생성합니다.

소스:
  Layer 1 (API 관련법령): law_relationship_graph.json
  Layer 2 (LLM 상위법):  law_summaries_all.json → 관련_상위법
  Layer 3 (용어 매핑):   daily_to_term_dict.json + LLM 일상용어_동의어

출력:
  1. unified_law_graph.json     — 법률 간 관계 그래프 (확장 검색용)
  2. term_expansion_dict.json   — 용어 확장 사전 (쿼리 확장용)

사용법: python build_graph.py
"""

import json
import gzip
from paths import path_setting, output_path
import os
import re
from collections import defaultdict
from datetime import datetime

# ============================================================
# 경로 설정
# ============================================================
BASE_DIR = path_setting("SCIE_COLLECTION_DIR", "outputs/collected_terms")
SUMMARY_PATH = path_setting("SCIE_DICTIONARY_SUMMARIES", "data/summaries/dictionary_input_5170.json")
LAW_LIST_PATH = path_setting("SCIE_LAW_CATALOG", "data/corpus/law_catalog.json")

# 입력
GRAPH_PATH = path_setting("SCIE_API_LAW_GRAPH", "data/graphs/api_law_relationship_graph.json")
DAILY_TO_TERM_PATH = path_setting("SCIE_API_TERMS", "data/terms/api_relations.json.gz")
HIERARCHY_PATH = os.path.join(BASE_DIR, "step4_system", "law_hierarchy.json")

# 출력
OUTPUT_DIR = os.path.dirname(output_path("rebuilt_graphs/.root"))
os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_json(path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as f:
        return json.load(f)


def save_json(data, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")


def clean_law_name(name):
    """시행령/시행규칙 제거하여 모법명 추출"""
    if not name:
        return ""
    return str(name).replace(" 시행령", "").replace(" 시행규칙", "").strip()


# ============================================================
# 1. 통합 법률 관계 그래프 구축
# ============================================================
def build_unified_graph():
    print("=" * 60)
    print("  1. 통합 법률 관계 그래프 구축")
    print("=" * 60)

    # 그래프: {법률명: {관련법률명: {"relation": 유형, "level": 신뢰도, "source": 출처}}}
    graph = defaultdict(dict)

    # --- Layer 1: API 관련법령 (Level 1, 높은 신뢰도) ---
    print("\n[Layer 1] API 관련법령 로딩...")
    api_graph = load_json(GRAPH_PATH)
    edges = api_graph.get("edges", [])
    nodes = api_graph.get("nodes", {})

    l1_count = 0
    for e in edges:
        source = clean_law_name(e.get("source_name", ""))
        target = clean_law_name(e.get("target_name", ""))
        rel_type = e.get("relation_type", "")

        if not source or not target or source == target:
            continue

        # 하위법(시행령/시행규칙)은 clean_law_name으로 모법과 같아지므로 스킵됨
        if "하위법" in rel_type:
            continue

        graph[source][target] = {
            "relation": rel_type,
            "level": 1,
            "source": "API관련법령"
        }
        # 역방향도 추가
        reverse_rel = _reverse_relation(rel_type)
        graph[target][source] = {
            "relation": reverse_rel,
            "level": 1,
            "source": "API관련법령"
        }
        l1_count += 1

    print(f"  Layer 1 엣지: {l1_count}개 (양방향)")

    # --- Layer 2: LLM 요약 관련_상위법 (Level 3, 검증 필요) ---
    print("\n[Layer 2] LLM 요약 관련_상위법 로딩...")
    summaries = load_json(SUMMARY_PATH)

    # 법령명 → MST 매핑 (정확 매칭용)
    law_list = load_json(LAW_LIST_PATH)
    name_to_mst = {}
    mst_to_name = {}
    all_law_names = set()
    for law in law_list:
        name = law.get("법령명한글", "")
        mst = law.get("법령MST", "")
        name_to_mst[name] = mst
        mst_to_name[mst] = name
        all_law_names.add(name)
        all_law_names.add(clean_law_name(name))

    l2_count = 0
    l2_verified = 0
    for s in summaries:
        law_name = s.get("법령명", "")
        base_name = clean_law_name(law_name)
        upper_laws = s.get("관련_상위법", [])

        if not upper_laws or not isinstance(upper_laws, list):
            continue

        for upper in upper_laws:
            if not upper or not isinstance(upper, str):
                continue

            # 상위법 이름 정리 (괄호 설명 제거)
            upper_clean = re.sub(r'\(.*?\)', '', upper).strip()

            # 실제 존재하는 법령인지 확인
            matched_name = _find_law_name(upper_clean, all_law_names)
            if not matched_name:
                continue

            matched_base = clean_law_name(matched_name)
            if matched_base == base_name or not matched_base:
                continue

            # API 그래프에도 있으면 Level 1로 이미 처리됨 → 스킵
            if matched_base in graph.get(base_name, {}):
                if graph[base_name][matched_base]["level"] == 1:
                    l2_verified += 1
                    continue

            graph[base_name][matched_base] = {
                "relation": "LLM상위법",
                "level": 3,
                "source": "LLM요약"
            }
            graph[matched_base][base_name] = {
                "relation": "LLM하위법",
                "level": 3,
                "source": "LLM요약"
            }
            l2_count += 1

    print(f"  Layer 2 엣지: {l2_count}개 (신규), API와 겹침: {l2_verified}개")

    # --- 통계 ---
    total_nodes = set()
    total_edges = 0
    for src, targets in graph.items():
        total_nodes.add(src)
        for tgt in targets:
            total_nodes.add(tgt)
            total_edges += 1

    level_counts = defaultdict(int)
    for src, targets in graph.items():
        for tgt, info in targets.items():
            level_counts[info["level"]] += 1

    print(f"\n  === 통합 그래프 통계 ===")
    print(f"  노드: {len(total_nodes)}개")
    print(f"  엣지: {total_edges}개")
    print(f"  Level 1 (API): {level_counts[1]}개")
    print(f"  Level 3 (LLM): {level_counts[3]}개")

    # 저장
    graph_output = {
        "graph": {k: v for k, v in graph.items()},  # defaultdict → dict
        "stats": {
            "nodes": len(total_nodes),
            "edges": total_edges,
            "level_1_edges": level_counts[1],
            "level_3_edges": level_counts[3],
            "built_at": datetime.now().isoformat()
        }
    }
    save_json(graph_output, os.path.join(OUTPUT_DIR, "unified_law_graph.json"))

    return graph


def _reverse_relation(rel_type):
    """관계 유형의 역방향"""
    if "기본법" in rel_type:
        return "개별법(역)"
    elif "특별법" in rel_type:
        return "일반법(역)"
    elif "개별법" in rel_type:
        return "기본법(역)"
    elif "상위법" in rel_type:
        return "하위법(역)"
    elif "일반법" in rel_type:
        return "특별법(역)"
    return rel_type + "(역)"


def _find_law_name(query, all_names):
    """법령명 퍼지 매칭 - 정확매칭 → 포함매칭"""
    if not query:
        return None

    # 1. 정확 매칭
    if query in all_names:
        return query

    # 2. 포함 매칭 (짧은 이름이 긴 이름에 포함)
    candidates = [n for n in all_names if query in n and len(n) < len(query) + 20]
    if len(candidates) == 1:
        return candidates[0]

    # 3. 역방향 포함 (긴 이름의 일부가 query)
    candidates = [n for n in all_names if n in query]
    if candidates:
        return max(candidates, key=len)  # 가장 긴 매칭

    return None


# ============================================================
# 2. 용어 확장 사전 구축
# ============================================================
def build_term_expansion_dict():
    print("\n" + "=" * 60)
    print("  2. 용어 확장 사전 구축")
    print("=" * 60)

    # 용어 사전: {일상용어: [법령용어1, 법령용어2, ...]}
    expansion = defaultdict(set)

    # --- Source 1: Step 6 일상용어→법령용어 (API 공식 데이터) ---
    print("\n[Source 1] API 일상용어→법령용어...")
    d2t = load_json(DAILY_TO_TERM_PATH)

    s1_count = 0
    for daily_word, mappings in d2t.items():
        if isinstance(mappings, list):
            for m in mappings:
                term = m.get("법령용어", "")
                rel = m.get("관계", "")
                if term and rel in ("동의어", "연관어", "상위어", "하위어"):
                    expansion[daily_word].add(term)
                    # 역방향도 추가
                    expansion[term].add(daily_word)
                    s1_count += 1
        elif isinstance(mappings, dict) and not mappings.get("_raw"):
            # 구조가 다른 경우
            for k, v in mappings.items():
                if isinstance(v, str) and v:
                    expansion[daily_word].add(v)

    print(f"  Source 1: {s1_count}개 매핑, {len(expansion)}개 용어")

    # --- Source 2: LLM 요약의 일상용어_동의어 ---
    print("\n[Source 2] LLM 일상용어_동의어...")
    summaries = load_json(SUMMARY_PATH)

    s2_count = 0
    for s in summaries:
        synonyms = s.get("일상용어_동의어", [])
        if not synonyms or not isinstance(synonyms, list):
            continue

        for syn_entry in synonyms:
            if not isinstance(syn_entry, str):
                continue

            # "지반침하=싱크홀,땅꺼짐" 형식 파싱
            if "=" in syn_entry:
                parts = syn_entry.split("=", 1)
                legal_term = parts[0].strip()
                daily_terms = [t.strip() for t in parts[1].replace("·", ",").replace("、", ",").split(",") if t.strip()]

                for dt in daily_terms:
                    expansion[dt].add(legal_term)
                    expansion[legal_term].add(dt)
                    s2_count += 1

                # 일상용어끼리도 연결
                for dt in daily_terms:
                    for dt2 in daily_terms:
                        if dt != dt2:
                            expansion[dt].add(dt2)

    print(f"  Source 2: {s2_count}개 매핑")

    # --- Source 3: LLM 요약의 규율분야_키워드 → 법령명 연결 ---
    print("\n[Source 3] LLM 규율분야_키워드...")
    keyword_to_laws = defaultdict(set)
    for s in summaries:
        law_name = clean_law_name(s.get("법령명", ""))
        keywords = s.get("규율분야_키워드", [])
        if keywords and isinstance(keywords, list):
            for kw in keywords:
                if isinstance(kw, str) and kw:
                    keyword_to_laws[kw].add(law_name)

    print(f"  Source 3: {len(keyword_to_laws)}개 키워드 → 법률 매핑")

    # set → list 변환
    expansion_dict = {k: sorted(list(v)) for k, v in expansion.items()}

    print(f"\n  === 용어 확장 사전 통계 ===")
    print(f"  총 용어: {len(expansion_dict)}개")
    total_mappings = sum(len(v) for v in expansion_dict.values())
    print(f"  총 매핑: {total_mappings}개")

    # 핵심 용어 확인
    print(f"\n  [핵심 용어 확장 확인]")
    for term in ["싱크홀", "산불", "녹조", "가스누출", "해양오염", "침수", "지반침하"]:
        expanded = expansion_dict.get(term, [])
        print(f"    {term} → {expanded[:5]}{'...' if len(expanded)>5 else ''}")

    # 저장
    output = {
        "expansion": expansion_dict,
        "keyword_to_laws": {k: sorted(list(v)) for k, v in keyword_to_laws.items()},
        "stats": {
            "total_terms": len(expansion_dict),
            "total_mappings": total_mappings,
            "keyword_law_mappings": len(keyword_to_laws),
            "built_at": datetime.now().isoformat()
        }
    }
    save_json(output, os.path.join(OUTPUT_DIR, "term_expansion_dict.json"))

    return expansion_dict, keyword_to_laws


# ============================================================
# 메인
# ============================================================
if __name__ == "__main__":
    print(f"통합 그래프 + 용어 사전 구축 시작: {datetime.now()}")

    graph = build_unified_graph()
    expansion, kw_to_laws = build_term_expansion_dict()

    # 핵심 검증: 싱크홀 사례
    print("\n" + "=" * 60)
    print("  핵심 검증: 싱크홀 사례 시뮬레이션")
    print("=" * 60)

    query_terms = ["싱크홀", "지반침하", "도로함몰", "공동구"]
    print(f"\n  쿼리 키워드: {query_terms}")

    # Step A: 용어 확장
    expanded_terms = set(query_terms)
    for t in query_terms:
        if t in expansion:
            expanded_terms.update(expansion[t])
    print(f"  용어 확장 후: {sorted(expanded_terms)}")

    # Step B: 가상 검색 결과 (지하안전관리법을 찾았다고 가정)
    found_laws = ["지하안전관리에 관한 특별법", "시설물의 안전 및 유지관리에 관한 특별법"]
    print(f"  검색된 법률: {found_laws}")

    # Step C: 그래프 확장
    expanded_laws = set(found_laws)
    for law in found_laws:
        base = clean_law_name(law)
        if base in graph:
            for related, info in graph[base].items():
                expanded_laws.add(related)
                print(f"    + {related} [{info['relation']}, Level {info['level']}]")

    print(f"\n  최종 법률 집합 ({len(expanded_laws)}개): {sorted(expanded_laws)}")

    # 정답 확인
    true_laws = {"지하안전관리에 관한 특별법", "시설물의 안전 및 유지관리에 관한 특별법",
                 "공동구 설치 및 관리지침", "국토의 계획 및 이용에 관한 법률", "재난 및 안전관리 기본법"}
    hit = true_laws.intersection(expanded_laws)
    miss = true_laws - expanded_laws
    print(f"\n  정답 Hit: {hit}")
    print(f"  정답 Miss: {miss}")

    print(f"\n완료: {datetime.now()}")