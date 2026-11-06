"""
법령 관계 그래프 데이터 수집 스크립트 (v3)
==========================================
Step 1: 관련법령 수집 (1,703개 법률의 관련법령)
Step 2: 법령용어 목록 수집 (지식베이스 lstrmAl - 더 풍부한 데이터)
Step 3: 일상용어 목록 수집 (dlytrm - 전체 일상용어)
Step 4: 법령체계도 전체 수집
Step 5: 법령용어→일상용어 연계 수집 (lstrmRlt)
Step 6: 일상용어→법령용어 역방향 연계 수집 (dlytrmRlt)

사용법:
  python collect_law_graph_v3.py --step 1
  python collect_law_graph_v3.py --step all
  
추천 실행 순서: 4 → 2 → 3 → 1 → 5 → 6
"""

import argparse
from paths import path_setting, output_path
import json
import os
import time
import requests
import logging
from datetime import datetime
from collections import Counter
from pathlib import Path

# ============================================================
# 설정
# ============================================================
OC = os.environ.get("LAW_API_OC", "")
BASE_DIR = str(Path(output_path("collected_terms/.root")).parent)
LAW_LIST_PATH = path_setting("SCIE_LAW_CATALOG", "data/corpus/law_catalog.json")

URL_SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
URL_SERVICE = "https://www.law.go.kr/DRF/lawService.do"

REQUEST_DELAY = 0.5
MAX_RETRIES = 3
RETRY_DELAY = 5

# ============================================================
# 로깅
# ============================================================
os.makedirs(BASE_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, 'collect_log.txt'), mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


# ============================================================
# 유틸리티
# ============================================================
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def safe_request(url, params, timeout=30):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning(f"  요청 실패 (시도 {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


def save_json(data, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    size = len(data) if isinstance(data, (list, dict)) else '?'
    logger.info(f"저장: {filepath} ({size}건)")


def load_json(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def load_law_list():
    """법령목록에서 법률(1,703건)만 추출"""
    with open(LAW_LIST_PATH, 'r', encoding='utf-8') as f:
        all_laws = json.load(f)
    laws_only = [d for d in all_laws if d.get('법령구분') == '법률']
    logger.info(f"전체 법령: {len(all_laws)}건, 법률만: {len(laws_only)}건")
    return laws_only, all_laws


def paginated_collect(target, url=None, extra_params=None, label=""):
    """
    페이지네이션 공통 함수
    display=100, page 순회하여 전체 데이터 수집
    """
    if url is None:
        url = URL_SEARCH
    
    all_items = []
    page = 1
    total_cnt = None
    
    while True:
        logger.info(f"  [{label}] 페이지 {page} 수집...")
        params = {
            "OC": OC, "target": target, "type": "JSON",
            "display": 100, "page": page
        }
        if extra_params:
            params.update(extra_params)
        
        resp = safe_request(url, params)
        if not resp:
            logger.error(f"  페이지 {page} 요청 실패, 중단")
            break
        
        try:
            data = resp.json()
        except:
            logger.error(f"  페이지 {page} JSON 파싱 실패")
            break
        
        # 첫 페이지: 구조 확인
        if page == 1:
            logger.info(f"  응답 최상위 키: {list(data.keys())}")
        
        # 중첩 dict에서 실제 데이터 찾기
        search_data = data
        for k, v in data.items():
            if isinstance(v, dict) and any(
                sk in v for sk in ['totalCnt', 'numOfRows', 'page']
            ):
                search_data = v
                break
        
        # totalCnt
        if total_cnt is None:
            total_cnt = int(search_data.get("totalCnt", search_data.get("검색결과개수", 0)))
            logger.info(f"  전체: {total_cnt}건")
            if total_cnt == 0:
                logger.warning(f"  데이터 없음!")
                break
        
        # 리스트 항목 찾기
        items = None
        for k, v in search_data.items():
            if isinstance(v, list):
                items = v
                if page == 1:
                    logger.info(f"  항목 키: '{k}', 첫 항목: {json.dumps(v[0], ensure_ascii=False)[:200] if v else 'empty'}")
                break
        
        # 단일 dict인 경우 (결과가 1개)
        if items is None:
            for k, v in search_data.items():
                if isinstance(v, dict) and 'id' in v:
                    items = [v]
                    break
        
        if not items:
            logger.warning(f"  페이지 {page}: 항목 없음, 중단")
            if page == 1:
                logger.info(f"  search_data: {json.dumps(search_data, ensure_ascii=False)[:500]}")
            break
        
        all_items.extend(items)
        logger.info(f"  페이지 {page}: {len(items)}건 (누적: {len(all_items)}/{total_cnt})")
        
        if len(all_items) >= total_cnt or len(items) < 100:
            break
        
        page += 1
    
    return all_items, total_cnt


# ============================================================
# Step 1: 관련법령 수집
# ============================================================
def step1_related_laws():
    logger.info("=" * 60)
    logger.info("Step 1: 관련법령 수집 (1,703개 법률)")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step1_related_laws")
    ensure_dir(output_dir)
    
    result_file = os.path.join(output_dir, "related_laws_raw.json")
    progress_file = os.path.join(output_dir, "_progress.json")
    
    laws_only, _ = load_law_list()
    
    results = load_json(result_file) or {}
    progress = load_json(progress_file) or {"completed": [], "failed": []}
    completed_set = set(progress["completed"])
    
    logger.info(f"이미 완료: {len(completed_set)}건, 남은: {len(laws_only) - len(completed_set)}건")
    
    for i, law in enumerate(laws_only):
        name = law["법령명한글"]
        mst = law["법령MST"]  # API에서 사용하는 법령ID
        
        if mst in completed_set:
            continue
        
        logger.info(f"[{i+1}/{len(laws_only)}] {name} (MST: {mst})")
        
        # 방법 1: MST(=API법령ID)로 조회
        data = None
        resp = safe_request(URL_SEARCH, {
            "OC": OC, "target": "lsRlt", "type": "JSON", "ID": mst
        })
        if resp:
            try:
                data = resp.json()
                if "일치하는" in str(data.get("Law", "")):
                    data = None
            except:
                data = None
        
        # 방법 2: 법령명으로 조회
        if data is None:
            resp = safe_request(URL_SEARCH, {
                "OC": OC, "target": "lsRlt", "type": "JSON", "query": name
            })
            if resp:
                try:
                    data = resp.json()
                    if "일치하는" in str(data.get("Law", "")):
                        data = None
                except:
                    data = None
        
        results[mst] = {"법령명": name, "법령MST": mst, "response": data}
        
        if data is None:
            progress["failed"].append({"mst": mst, "name": name})
        
        progress["completed"].append(mst)
        completed_set.add(mst)
        
        # 50건마다 중간저장
        if (i + 1) % 50 == 0:
            save_json(results, result_file)
            save_json(progress, progress_file)
            logger.info(f"  중간저장 ({len(completed_set)}건)")
    
    save_json(results, result_file)
    save_json(progress, progress_file)
    
    # 그래프 변환
    graph = _build_related_graph(results)
    save_json(graph, os.path.join(output_dir, "law_relationship_graph.json"))
    _print_step1_stats(graph, progress)


def _build_related_graph(raw):
    nodes = {}
    edges = []
    
    for mst, item in raw.items():
        resp = item.get("response")
        if not resp:
            continue
        
        search = resp.get("lsRltSearch", {})
        law_data = search.get("법령", {})
        if not law_data:
            continue
        
        base_id = law_data.get("기준법령ID", "")
        base_name = law_data.get("기준법령명", "")
        nodes[base_id] = base_name
        
        related = law_data.get("관련법령", [])
        if isinstance(related, dict):
            related = [related]
        
        for r in related:
            rid = r.get("관련법령ID", "")
            rname = r.get("관련법령명", "")
            rcode = r.get("법령간관계코드", "")
            rtype = r.get("법령간관계", "")
            nodes[rid] = rname
            edges.append({
                "source_id": base_id, "source_name": base_name,
                "target_id": rid, "target_name": rname,
                "relation_code": rcode, "relation_type": rtype
            })
    
    return {"nodes": nodes, "edges": edges, "collected_at": datetime.now().isoformat()}


def _print_step1_stats(graph, progress):
    types = Counter(e["relation_type"] for e in graph["edges"])
    has_related = set(e["source_id"] for e in graph["edges"])
    failed = len(progress.get("failed", []))
    
    logger.info(f"\n=== Step 1 통계 ===")
    logger.info(f"노드: {len(graph['nodes'])}개, 엣지: {len(graph['edges'])}개")
    logger.info(f"관련법령 있는 법률: {len(has_related)}개, 없는 법률: {failed}개")
    logger.info(f"관계 유형:")
    for t, c in types.most_common():
        logger.info(f"  {t}: {c}건")


# ============================================================
# Step 2: 법령용어 목록 수집 (지식베이스 lstrmAl)
# ============================================================
def step2_term_list():
    logger.info("=" * 60)
    logger.info("Step 2: 법령용어 목록 수집 (지식베이스 lstrmAl)")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step2_legal_terms")
    ensure_dir(output_dir)
    
    all_terms, total = paginated_collect("lstrmAl", label="법령용어")
    save_json(all_terms, os.path.join(output_dir, "legal_terms_all.json"))
    
    logger.info(f"Step 2 완료: {len(all_terms)}개 법령용어 (총 {total})")
    
    # 샘플 출력
    for t in all_terms[:3]:
        logger.info(f"  샘플: {json.dumps(t, ensure_ascii=False)[:300]}")


# ============================================================
# Step 3: 일상용어 목록 수집 (dlytrm)
# ============================================================
def step3_daily_term_list():
    logger.info("=" * 60)
    logger.info("Step 3: 일상용어 목록 수집 (dlytrm)")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step3_daily_terms")
    ensure_dir(output_dir)
    
    all_terms, total = paginated_collect("dlytrm", label="일상용어")
    save_json(all_terms, os.path.join(output_dir, "daily_terms_all.json"))
    
    logger.info(f"Step 3 완료: {len(all_terms)}개 일상용어 (총 {total})")
    
    for t in all_terms[:3]:
        logger.info(f"  샘플: {json.dumps(t, ensure_ascii=False)[:300]}")


# ============================================================
# Step 4: 법령체계도 전체 수집
# ============================================================
def step4_system():
    logger.info("=" * 60)
    logger.info("Step 4: 법령체계도 전체 수집")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step4_system")
    ensure_dir(output_dir)
    
    all_laws, total = paginated_collect("lsStmd", label="체계도")
    save_json(all_laws, os.path.join(output_dir, "system_all.json"))
    
    # 계층 구조 추출
    hierarchy = _build_hierarchy(all_laws)
    save_json(hierarchy, os.path.join(output_dir, "law_hierarchy.json"))
    
    logger.info(f"Step 4 완료: {len(all_laws)}건, {len(hierarchy)}개 법률 그룹")


def _build_hierarchy(all_laws):
    laws_only = [l for l in all_laws if l.get("법령구분명") == "법률"]
    hierarchy = {}
    
    for law in laws_only:
        name = law.get("법령명", "")
        hierarchy[name] = {"법률": law, "하위법령": []}
    
    # 시행령/시행규칙 매칭: 긴 법률명부터 매칭 (서브스트링 충돌 방지)
    sorted_names = sorted(hierarchy.keys(), key=len, reverse=True)
    
    non_laws = [l for l in all_laws if l.get("법령구분명") != "법률"]
    matched = 0
    for sub in non_laws:
        sub_name = sub.get("법령명", "")
        for base_name in sorted_names:
            if sub_name.startswith(base_name):
                hierarchy[base_name]["하위법령"].append(sub)
                matched += 1
                break
    
    logger.info(f"  법률: {len(laws_only)}건, 매칭 하위법령: {matched}건")
    return hierarchy


# ============================================================
# Step 5: 법령용어 → 일상용어 연계 수집 (lstrmRlt)
# ============================================================
def step5_term_to_daily():
    logger.info("=" * 60)
    logger.info("Step 5: 법령용어→일상용어 연계 수집")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step5_term_to_daily")
    ensure_dir(output_dir)
    
    # Step 2 결과 로드
    term_file = os.path.join(BASE_DIR, "step2_legal_terms", "legal_terms_all.json")
    if not os.path.exists(term_file):
        logger.error("Step 2를 먼저 실행하세요!")
        return
    
    all_terms = load_json(term_file)
    
    # 용어명 추출
    term_names = []
    for t in all_terms:
        if isinstance(t, dict):
            name = t.get("법령용어명", "")
            if not name:
                # 키 이름 동적 탐색
                for k, v in t.items():
                    if "용어" in k and "명" in k and isinstance(v, str) and v:
                        name = v
                        break
            if name:
                term_names.append(name)
    
    term_names = list(dict.fromkeys(term_names))  # 중복 제거, 순서 유지
    logger.info(f"조회 대상 법령용어: {len(term_names)}개")
    
    result_file = os.path.join(output_dir, "term_to_daily_raw.json")
    progress_file = os.path.join(output_dir, "_progress.json")
    
    results = load_json(result_file) or {}
    progress = load_json(progress_file) or {"completed": []}
    completed_set = set(progress["completed"])
    
    logger.info(f"이미 완료: {len(completed_set)}건")
    
    found_count = 0
    for i, name in enumerate(term_names):
        if name in completed_set:
            continue
        
        if (i + 1) % 200 == 0:
            logger.info(f"[{i+1}/{len(term_names)}] {name} (연계 발견: {found_count}건)")
        
        resp = safe_request(URL_SERVICE, {
            "OC": OC, "target": "lstrmRlt", "type": "JSON", "query": name
        })
        
        if resp:
            try:
                data = resp.json()
                if "일치하는" not in str(data.get("Law", "")):
                    results[name] = data
                    found_count += 1
            except:
                pass
        
        progress["completed"].append(name)
        completed_set.add(name)
        
        if (i + 1) % 300 == 0:
            save_json(results, result_file)
            save_json(progress, progress_file)
    
    save_json(results, result_file)
    save_json(progress, progress_file)
    
    # 동의어 사전 추출
    synonym = _extract_term_to_daily(results)
    save_json(synonym, os.path.join(output_dir, "synonym_dict.json"))
    
    logger.info(f"Step 5 완료: {len(results)}건 연계, {len(synonym)}개 매핑")
    _print_synonym_stats(synonym)


def _extract_term_to_daily(raw):
    result = {}
    for term_name, data in raw.items():
        service = data.get("lstrmRltService", {})
        term_data = service.get("법령용어", {})
        related = term_data.get("연계용어", [])
        if isinstance(related, dict):
            related = [related]
        
        mappings = []
        for r in related:
            mappings.append({
                "일상용어": r.get("일상용어명", ""),
                "관계": r.get("용어관계", ""),
                "코드": r.get("용어관계코드", "")
            })
        if mappings:
            result[term_name] = mappings
    return result


def _print_synonym_stats(synonym):
    all_relations = []
    for mappings in synonym.values():
        for m in mappings:
            all_relations.append(m.get("관계", ""))
    
    rel_counts = Counter(all_relations)
    logger.info(f"용어 관계 분포:")
    for r, c in rel_counts.most_common():
        logger.info(f"  {r}: {c}건")


# ============================================================
# Step 6: 일상용어 → 법령용어 역방향 연계 수집 (dlytrmRlt)
# ============================================================
def step6_daily_to_term():
    logger.info("=" * 60)
    logger.info("Step 6: 일상용어→법령용어 역방향 연계 수집")
    logger.info("=" * 60)
    
    output_dir = os.path.join(BASE_DIR, "step6_daily_to_term")
    ensure_dir(output_dir)
    
    # Step 3 결과에서 전체 일상용어 목록 로드
    daily_file = os.path.join(BASE_DIR, "step3_daily_terms", "daily_terms_all.json")
    if not os.path.exists(daily_file):
        logger.error("Step 3를 먼저 실행하세요!")
        return
    
    all_daily = load_json(daily_file)
    
    # 일상용어명 추출
    daily_names = []
    for t in all_daily:
        if isinstance(t, dict):
            name = t.get("일상용어명", "")
            if not name:
                for k, v in t.items():
                    if "일상" in k and "명" in k and isinstance(v, str) and v:
                        name = v
                        break
            if name:
                daily_names.append(name)
    
    daily_names = list(dict.fromkeys(daily_names))  # 중복 제거
    
    # 평가 데이터 키워드 추가
    eval_kw = _load_eval_keywords()
    if eval_kw:
        existing = set(daily_names)
        added = [kw for kw in eval_kw if kw not in existing]
        daily_names.extend(added)
        logger.info(f"평가 키워드 {len(added)}개 추가")
    
    logger.info(f"조회 대상 일상용어: {len(daily_names)}개")
    
    result_file = os.path.join(output_dir, "daily_to_term_raw.json")
    progress_file = os.path.join(output_dir, "_progress.json")
    
    results = load_json(result_file) or {}
    progress = load_json(progress_file) or {"completed": []}
    completed_set = set(progress["completed"])
    
    logger.info(f"이미 완료: {len(completed_set)}건")
    
    found_count = 0
    for i, name in enumerate(daily_names):
        if name in completed_set:
            continue
        
        if (i + 1) % 200 == 0:
            logger.info(f"[{i+1}/{len(daily_names)}] {name} (연계 발견: {found_count}건)")
        
        resp = safe_request(URL_SERVICE, {
            "OC": OC, "target": "dlytrmRlt", "type": "JSON", "query": name
        })
        
        if resp:
            try:
                data = resp.json()
                # 실패 응답 체크 (다양한 패턴)
                raw_str = json.dumps(data, ensure_ascii=False)
                if "일치하는" not in raw_str and "없습니다" not in raw_str:
                    results[name] = data
                    found_count += 1
            except:
                pass
        
        progress["completed"].append(name)
        completed_set.add(name)
        
        if (i + 1) % 300 == 0:
            save_json(results, result_file)
            save_json(progress, progress_file)
    
    save_json(results, result_file)
    save_json(progress, progress_file)
    
    # 역방향 사전 추출
    reverse = _extract_daily_to_term(results)
    save_json(reverse, os.path.join(output_dir, "daily_to_term_dict.json"))
    
    logger.info(f"Step 6 완료: {len(results)}건 연계, {len(reverse)}개 역방향 매핑")


def _extract_daily_to_term(raw):
    """일상용어→법령용어 매핑 추출"""
    result = {}
    
    for daily_word, data in raw.items():
        if not isinstance(data, dict):
            continue
        
        # 응답 구조 탐색 (dlytrmRlt 응답 구조)
        mappings = []
        
        # 패턴 1: dlytrmRltService 키
        for top_key, top_val in data.items():
            if not isinstance(top_val, dict):
                continue
            
            # 일상용어 정보
            daily_info = None
            for k, v in top_val.items():
                if isinstance(v, dict) and ("일상" in k or "용어" in k):
                    daily_info = v
                    break
                elif isinstance(v, list):
                    # 리스트 직접 포함
                    for item in v:
                        if isinstance(item, dict):
                            term = item.get("법령용어명", "")
                            rel = item.get("용어관계", "")
                            code = item.get("용어관계코드", "")
                            if term:
                                mappings.append({
                                    "법령용어": term,
                                    "관계": rel,
                                    "코드": code
                                })
            
            if daily_info:
                related = daily_info.get("연계용어", [])
                if isinstance(related, dict):
                    related = [related]
                for r in related:
                    term = r.get("법령용어명", "")
                    rel = r.get("용어관계", "")
                    code = r.get("용어관계코드", "")
                    if term:
                        mappings.append({
                            "법령용어": term,
                            "관계": rel,
                            "코드": code
                        })
        
        if mappings:
            result[daily_word] = mappings
        else:
            # 구조 파악 못한 경우 raw 보존
            result[daily_word] = {"_raw": True, "data": data}
    
    return result


def _load_eval_keywords():
    """평가 데이터에서 키워드 추출"""
    eval_path = path_setting("SCIE_KEYWORD_CSV", "local_inputs/legacy_keyword_evaluation.csv")
    if not os.path.exists(eval_path):
        return set()
    try:
        import csv
        keywords = set()
        with open(eval_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                for col in row:
                    if '키워드' in col:
                        kws = row[col].replace(',', ' ').replace('/', ' ').split()
                        keywords.update(kw.strip() for kw in kws if kw.strip())
        logger.info(f"평가 키워드 {len(keywords)}개 추출")
        return keywords
    except Exception as e:
        logger.warning(f"평가 데이터 로드 실패: {e}")
        return set()


# ============================================================
# 메인
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="법령 관계 그래프 데이터 수집 v3")
    parser.add_argument("--step", type=str, default="all",
                        help="실행할 단계 (1~6 또는 all)")
    args = parser.parse_args()
    if not OC:
        parser.error("Set LAW_API_OC before collecting")
    
    logger.info(f"{'='*60}")
    logger.info(f"법령 관계 그래프 수집 v3 시작: {datetime.now()}")
    logger.info(f"저장: {BASE_DIR}")
    logger.info(f"{'='*60}")
    
    steps = {
        "1": ("관련법령", step1_related_laws),
        "2": ("법령용어 목록", step2_term_list),
        "3": ("일상용어 목록", step3_daily_term_list),
        "4": ("법령체계도", step4_system),
        "5": ("법령용어→일상용어", step5_term_to_daily),
        "6": ("일상용어→법령용어", step6_daily_to_term),
    }
    
    # 추천 실행 순서
    run_order = ["4", "2", "3", "1", "5", "6"]
    
    if args.step == "all":
        for s in run_order:
            name, func = steps[s]
            logger.info(f"\n>>> Step {s}: {name} 시작")
            func()
    elif args.step in steps:
        name, func = steps[args.step]
        func()
    else:
        logger.error(f"잘못된 step: {args.step} (1~6 또는 all)")
    
    logger.info(f"\n완료: {datetime.now()}")


if __name__ == "__main__":
    main()