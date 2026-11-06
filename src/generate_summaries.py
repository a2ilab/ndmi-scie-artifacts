"""
법률 요약 생성 스크립트 v4 (gpt-oss:120b + Ollama)

본인 작동 코드(rag_aug_oss120b.py) 기반 + 보강:
- 반복 출력 감지 및 재시도
- JSON 형식 미준수 시 강화된 파서로 복구
- 중괄호 카운팅으로 JSON 뒤 쓰레기 텍스트 제거
- 10건마다 중간 저장 + 이어서 처리
- (추가) 법률, 시행령, 시행규칙 통합 지원 및 제1~3조 탐색 강화

사용법:
  python generate_summaries.py --input law_fulltext_all.json --output test.json --limit 3
  python generate_summaries.py --input law_fulltext_all.json --output law_summaries_all.json
"""

import json
import os
from paths import path_setting, output_path as output_file_path
import re
import time
import argparse
import requests
from pathlib import Path
from tqdm import tqdm


# ============================================================
# 설정
# ============================================================
OLLAMA_URL = os.environ.get("OLLAMA_URL") or "http://localhost:11434/api/chat"
MODEL_NAME = "gpt-oss:120b"
NUM_CTX = 32768
MAX_LAW_TEXT_CHARS = 40000
MAX_RETRIES = 3
SLEEP_BETWEEN_CALLS = 1


# ============================================================
# 프롬프트 (수정됨: 법률+시행령+시행규칙 통합 및 1~3조 강조)
# ============================================================
SYSTEM_PROMPT = (
    "Reasoning: medium\n"
    "당신은 한국의 법령(법률, 시행령, 시행규칙)을 일반 시민이 쉽게 검색할 수 있도록 요약하는 전문가입니다. "
    "반드시 지정된 JSON 형식으로만 응답하세요. "
    "JSON 외의 텍스트, 설명, 마크다운 코드블록(```)은 절대 포함하지 마세요."
)

USER_PROMPT_TEMPLATE = """다음 법령을 읽고 검색용 요약 JSON을 생성하세요.

[법령 정보]
법령명: {law_name}
법령MST: {law_mst}
법령구분: {law_type}

[법령 전문]
{law_text}

[출력 - 아래 JSON 형식만 출력. 다른 텍스트 금지]
{{
  "법령MST": "{law_mst}",
  "법령명": "{law_name}",
  "법령유형태그": "{law_type}",
  "핵심목적": "이 법령이 왜 존재하는지 1~2문장, 일상 언어",
  "적용대상_사고상황": ["구체적 사고/상황을 일상용어로 나열"],
  "일상용어_동의어": ["법령전문용어=일상표현 형태로 나열"],
  "규율분야_키워드": ["분야 키워드"],
  "관련_상위법": ["제1조~제3조에 언급된 근거 법률 및 상위 법령명"],
  "검색용_요약문": "200~400자 요약. 일상용어와 사고상황을 반드시 포함"
}}

[작성 규칙]
1. 일상용어_동의어: 법령용어를 일반인 검색어로 변환 (예: 지반침하=싱크홀,땅꺼짐)
2. 적용대상_사고상황: 구체적 상황 (예: 어선 전복, 바다 기름 유출)
3. 검색용_요약문: 백과사전처럼 쉽게 설명
4. 관련_상위법: 제1조부터 제3조까지(주로 목적, 정의, 적용범위 등)의 내용을 모두 꼼꼼히 읽고, 근거가 되는 상위 법률이나 관련 법령을 빠짐없이 추출할 것"""


# ============================================================
# 법률 텍스트 전처리 (본인 코드와 동일)
# ============================================================
def extract_key_sections(fulltext: str, max_chars: int = MAX_LAW_TEXT_CHARS) -> str:
    if len(fulltext) <= max_chars:
        return fulltext

    lines = fulltext.split('\n')
    key_parts = []
    article_titles = []
    in_key_article = False
    key_article_nums = set(f'제{i}조' for i in range(1, 11))

    for line in lines:
        stripped = line.strip()
        if any(kw in stripped for kw in key_article_nums):
            in_key_article = True
            key_parts.append(line)
            continue
        if stripped.startswith('제') and '조' in stripped[:8]:
            in_key_article = False
            if '(' in stripped and ')' in stripped:
                title = stripped[:stripped.index(')') + 1]
            else:
                title = stripped[:30]
            article_titles.append(title)
            continue
        if in_key_article:
            key_parts.append(line)

    result = '\n'.join(key_parts)
    if article_titles:
        result += '\n\n[이하 조문 제목 목록]\n' + ', '.join(article_titles)
    return result[:max_chars]


# ============================================================
# [보강] JSON 파싱 - 중괄호 카운팅 + 오류 복구
# ============================================================
def extract_first_json(text: str) -> str | None:
    """중괄호 카운팅으로 첫 번째 완전한 JSON 객체만 추출.
    
    gpt-oss가 JSON 뒤에 추가 텍스트를 이어쓰는 문제 해결.
    """
    cleaned = text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1]
    if "```" in cleaned:
        cleaned = cleaned.split("```")[0]
    cleaned = cleaned.strip()

    start_idx = cleaned.find('{')
    if start_idx == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start_idx, len(cleaned)):
        char = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return cleaned[start_idx:i + 1]
    return None


def parse_json_response(text: str, law_name: str, law_mst: str, law_type: str):
    """JSON 추출 + 파싱 + 오류 복구"""
    json_str = extract_first_json(text)
    if json_str is None:
        return None

    # 1차: 직접 파싱
    try:
        parsed = json.loads(json_str)
        parsed.setdefault("법령MST", law_mst)
        parsed.setdefault("법령명", law_name)
        parsed.setdefault("법령유형태그", law_type)
        return parsed
    except json.JSONDecodeError:
        pass

    # 2차: trailing comma 수정
    try:
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
        parsed = json.loads(fixed)
        parsed.setdefault("법령MST", law_mst)
        parsed.setdefault("법령명", law_name)
        parsed.setdefault("법령유형태그", law_type)
        return parsed
    except json.JSONDecodeError:
        pass

    # 3차: 제어문자 제거
    try:
        fixed = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
        fixed = re.sub(r',\s*}', '}', fixed)
        fixed = re.sub(r',\s*]', ']', fixed)
        parsed = json.loads(fixed)
        parsed.setdefault("법령MST", law_mst)
        parsed.setdefault("법령명", law_name)
        parsed.setdefault("법령유형태그", law_type)
        return parsed
    except json.JSONDecodeError:
        return None


# ============================================================
# [보강] 반복 출력 감지
# ============================================================
def has_repetition(text: str, min_len: int = 30, min_repeats: int = 3) -> bool:
    """텍스트에 반복 패턴이 있는지 감지.
    
    동일한 30자 이상 구절이 3회 이상 반복되면 True.
    """
    if len(text) < min_len * min_repeats:
        return False
    
    for length in [50, 40, 30]:
        for start in range(0, len(text) - length, 10):
            pattern = text[start:start + length]
            count = text.count(pattern)
            if count >= min_repeats:
                return True
    return False


# ============================================================
# LLM API 호출 (본인 코드 기반 + 보강)
# ============================================================
def call_llm(law_name: str, law_mst: str, law_text: str, law_type: str) -> dict:
    trimmed_text = extract_key_sections(law_text)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        law_name=law_name,
        law_mst=law_mst,
        law_type=law_type,
        law_text=trimmed_text
    )

    # 본인 코드와 동일한 payload
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
        "think": "low",
        "options": {
            "num_predict": 1024,
            "num_ctx": NUM_CTX,
            "temperature": 0.15,
            "repeat_penalty": 1.2,
            "top_p": 0.9,
            "top_k": 40,
        }
    }

    result_text = ""

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(OLLAMA_URL, json=payload, timeout=300)
            response.raise_for_status()
            data = response.json()

            result_text = data.get("message", {}).get("content", "")

            # [보강] 빈 응답 체크
            if not result_text.strip():
                print(f"  [빈 응답] {law_name} (시도 {attempt+1}/{MAX_RETRIES})")
                time.sleep(3)
                continue

            # [보강] 반복 출력 감지 → temperature 올려서 재시도
            if has_repetition(result_text):
                print(f"  [반복 감지] {law_name} (시도 {attempt+1}/{MAX_RETRIES})")
                payload["options"]["temperature"] = 0.3 + (attempt * 0.1)
                time.sleep(2)
                continue

            # [보강] 강화된 JSON 파싱
            parsed = parse_json_response(result_text, law_name, law_mst, law_type)
            if parsed is not None:
                # 검색용_요약문이 너무 짧으면 경고 (하지만 저장은 함)
                summary = parsed.get("검색용_요약문", "")
                if len(summary) < 50:
                    print(f"  [짧은 요약] {law_name}: {len(summary)}자")
                return parsed

            print(f"  [파싱 실패] {law_name} (시도 {attempt+1}/{MAX_RETRIES})")
            print(f"  응답 앞 200자: {result_text[:200]}")

        except requests.exceptions.Timeout:
            print(f"  [타임아웃] {law_name} (시도 {attempt+1}/{MAX_RETRIES})")
            time.sleep(10)
        except requests.exceptions.RequestException as e:
            print(f"  [에러] {law_name}: {e} (시도 {attempt+1}/{MAX_RETRIES})")
            time.sleep(5)

    # 모든 재시도 실패
    return {
        "법령MST": law_mst,
        "법령명": law_name,
        "법령유형태그": law_type,
        "파싱실패": True,
        "원본응답": result_text[:500] if result_text else "응답 없음"
    }


# ============================================================
# 메인 (본인 코드 기반 + 보강)
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="법령 요약 생성 v4 (gpt-oss:120b)")
    parser.add_argument("--input", default=path_setting("SCIE_FULLTEXT_FILE", "local_inputs/law_fulltext_all.json"))
    parser.add_argument("--output", default=output_file_path("new_summaries.json"))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        laws = json.load(f)
    print(f"총 {len(laws)}개 법령 로드")

    output_path = Path(args.output)
    summaries = []
    processed_msts = set()

    if output_path.exists():
        with open(output_path, 'r', encoding='utf-8') as f:
            loaded_summaries = json.load(f)
        
        # '파싱실패'가 없는 성공한 데이터만 필터링하여 남김
        summaries = [s for s in loaded_summaries if not s.get("파싱실패")]
        processed_msts = {s.get("법령MST") for s in summaries}
        
        print(f"기존 성공 {len(summaries)}건 로드 (실패했던 건은 다시 처리합니다)")

    end_idx = len(laws) if args.limit is None else min(args.start + args.limit, len(laws))
    laws_to_process = [
        law for law in laws[args.start:end_idx]
        if law.get("법령MST") not in processed_msts
    ]

    print(f"처리 대상: {len(laws_to_process)}개")
    print(f"설정: model={MODEL_NAME}, num_ctx={NUM_CTX}, Reasoning=medium, think=low")
    print("-" * 60)

    if not laws_to_process:
        print("모두 처리 완료!")
        return

    for i, law in enumerate(tqdm(laws_to_process, desc="요약 생성")):
        law_name = law.get("법령명", "")
        law_mst = law.get("법령MST", "")
        law_text = law.get("전문", "")
        law_type = law.get("법령구분", "알수없음")

        if not law_text:
            print(f"  [건너뜀] {law_name}: 전문 없음")
            continue

        summary = call_llm(law_name, law_mst, law_text, law_type)
        summaries.append(summary)

        # 10건마다 중간 저장
        if (i + 1) % 10 == 0:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(summaries, f, ensure_ascii=False, indent=2)
            failed_so_far = sum(1 for s in summaries if s.get("파싱실패"))
            if failed_so_far:
                print(f"  중간 저장: {len(summaries)}건 (실패: {failed_so_far}건)")

        time.sleep(SLEEP_BETWEEN_CALLS)

    # 최종 저장
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    total = len(summaries)
    failed = sum(1 for s in summaries if s.get("파싱실패"))
    print(f"\n{'='*60}")
    print(f"완료! 총 {total}건, 성공 {total-failed}건, 실패 {failed}건")
    print(f"저장: {args.output}")

    if failed > 0:
        print(f"\n실패 목록:")
        for s in summaries:
            if s.get("파싱실패"):
                print(f"  - {s.get('법령명')} ({s.get('법령MST')})")


if __name__ == "__main__":
    main()
