"""
extract_law_fulltext.py — 법령(법률, 시행령, 시행규칙) 전체 전문 텍스트 추출
========================================================
사용법 (터미널):
  python extract_law_fulltext.py
"""

# %% [1] 설정 및 라이브러리
import os
from paths import path_setting, output_path
import re
import json
from bs4 import BeautifulSoup
from tqdm.auto import tqdm

# ★ 경로 설정 — 본인 환경에 맞게 수정
DATA_DIR = path_setting("SCIE_LAW_HTML", "local_inputs/law_html")                          # 법령 HTML 폴더
METADATA_FILE = path_setting("SCIE_LAW_CATALOG", "data/corpus/law_catalog.json")   # 메타데이터
OUTPUT_FILE = output_path("law_fulltext_all.json")          # 출력 파일 (이름 변경됨)

# %% [2] 법령(법률, 시행령, 시행규칙) 필터링
# %% [2] 법령(법률, 시행령, 시행규칙) 필터링
with open(METADATA_FILE, 'r', encoding='utf-8') as f:
    all_laws = json.load(f)

# 법률, 대통령령(시행령), 총리령, 각종 부령(시행규칙), 헌법 포함
target_laws = []
for d in all_laws:
    law_type = d.get('법령구분', '')
    
    # 1. 기본 법령 및 헌법
    if law_type in ['법률', '대통령령', '시행령', '총리령', '헌법']:
        target_laws.append(d)
    # 2. 모든 부처의 시행규칙 (~부령) 및 과거 부처령(~원령)
    elif law_type.endswith('부령') or law_type.endswith('원령'):
        target_laws.append(d)
    # (대법원규칙, 국회규칙 등은 자동으로 제외됨)

law_mst_set = {d['법령MST'] for d in target_laws}
law_mst_to_name = {d['법령MST']: d['법령명한글'] for d in target_laws}
law_mst_to_type = {d['법령MST']: d.get('법령구분', 'Unknown') for d in target_laws}

print(f"전체 법령 목록: {len(all_laws):,}개")
print(f"추출 대상(법률/시행령/시행규칙): {len(target_laws):,}개")

# %% [3] HTML 파일 매칭
html_files = [f for f in os.listdir(DATA_DIR) if f.endswith('.html')]
print(f"HTML 파일: {len(html_files):,}개")

# 파일명에서 MST 추출: _000058__해사안전기본법.html → 000058
def extract_mst(filename):
    m = re.search(r'_(\d{6})__', filename)
    return m.group(1) if m else None

# 대상 HTML만 필터
target_html_files = []
matched_msts = set()
for fn in html_files:
    mst = extract_mst(fn)
    if mst and mst in law_mst_set:
        target_html_files.append(fn)
        matched_msts.add(mst)

target_html_files.sort()

print(f"대상 HTML 매칭: {len(target_html_files):,}개 / {len(target_laws):,}개")

# 매칭 안 된 법령 확인
missing = law_mst_set - matched_msts
if missing:
    print(f"⚠️ 매칭 안 된 법령: {len(missing)}개")
    for mst in list(missing)[:5]:
        print(f"   {mst}: {law_mst_to_name[mst]}")
else:
    print("✅ 전체 매칭 완료!")

# %% [4] 전문 텍스트 추출 함수
def extract_fulltext(html_content):
    """
    법령 HTML에서 전체 조문 텍스트를 추출.
    """
    soup = BeautifulSoup(html_content, "lxml")
    
    texts = []
    
    # 방법 1: pgroup 기반
    pgroups = soup.find_all("div", class_="pgroup")
    if pgroups:
        for pg in pgroups:
            for p in pg.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if txt:
                    texts.append(txt)
    
    # 방법 2: pgroup이 없으면 본문 전체에서 추출
    if not texts:
        body = soup.find("body")
        if body:
            for p in body.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if txt:
                    texts.append(txt)
    
    # 방법 3: 그래도 없으면 전체 텍스트
    if not texts:
        full = soup.get_text(" ", strip=True)
        if full:
            texts.append(full)
    
    return "\n".join(texts)


def extract_article_count(html_content):
    """조문 개수 카운트 (통계용)"""
    soup = BeautifulSoup(html_content, "lxml")
    articles = soup.find_all("p", class_="pty1")
    count = 0
    for p in articles:
        txt = p.get_text(" ", strip=True)
        if re.search(r'제\d+조', txt):
            count += 1
    return count

def remove_appendix(text):
    """부칙 제거: 첫 번째 '부칙 <제' 이후 텍스트 삭제"""
    match = re.search(r'부칙\s*<제\d+호', text)
    if match:
        return text[:match.start()].rstrip()
    match = re.search(r'\n부칙\s*\n', text)
    if match:
        return text[:match.start()].rstrip()
    return text

# %% [5] 전체 추출 실행
results = []
errors = []

for fn in tqdm(target_html_files, desc="법령 전문 추출"):
    mst = extract_mst(fn)
    law_name = law_mst_to_name.get(mst, "Unknown")
    law_type = law_mst_to_type.get(mst, "Unknown") # 추가됨
    filepath = os.path.join(DATA_DIR, fn)
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            html = f.read()
        
        fulltext = extract_fulltext(html)
        fulltext = remove_appendix(fulltext)
        article_count = extract_article_count(html)
        
        results.append({
            "법령MST": mst,
            "법령명": law_name,
            "법령구분": law_type, # 추가됨 (요약 스크립트에서 활용 가능)
            "파일명": fn,
            "조문수": article_count,
            "텍스트길이": len(fulltext),
            "전문": fulltext,
        })
    except Exception as e:
        errors.append({"파일": fn, "에러": str(e)})

print(f"\n✅ 추출 완료: {len(results):,}개")
if errors:
    print(f"⚠️ 에러: {len(errors)}개")
    for e in errors[:3]:
        print(f"   {e}")

# %% [6] 통계 확인
text_lens = [r['텍스트길이'] for r in results]
art_counts = [r['조문수'] for r in results]

print(f"\n{'='*50}")
print(f"  법령 전문 추출 통계")
print(f"{'='*50}")
print(f"  총 법령:    {len(results):,}개")
print(f"  텍스트 길이:")
print(f"    최소: {min(text_lens):,}자")
print(f"    평균: {sum(text_lens)//len(text_lens):,}자")
print(f"    중앙: {sorted(text_lens)[len(text_lens)//2]:,}자")
print(f"    최대: {max(text_lens):,}자")
print(f"  조문 수:")
print(f"    최소: {min(art_counts)}")
print(f"    평균: {sum(art_counts)//len(art_counts)}")
print(f"    최대: {max(art_counts)}")

# 텍스트 길이 분포
brackets = [0, 1000, 5000, 10000, 30000, 50000, 100000, float('inf')]
labels = ['~1K', '1K~5K', '5K~10K', '10K~30K', '30K~50K', '50K~100K', '100K~']
print(f"\n  텍스트 길이 분포:")
for i in range(len(labels)):
    count = sum(1 for l in text_lens if brackets[i] <= l < brackets[i+1])
    bar = '█' * (count // 100) # 개수가 많아질 것을 대비해 막대그래프 스케일 조정 (10 -> 100)
    print(f"    {labels[i]:>10}: {count:>4}개 {bar}")

# %% [7] 샘플 확인
print(f"\n{'='*50}")
print(f"  샘플 확인")
print(f"{'='*50}")

# 안전 관련 법령 샘플
safety_keywords = ['안전', '재난', '소방', '산업안전']
for kw in safety_keywords:
    sample = next((r for r in results if kw in r['법령명']), None)
    if sample:
        print(f"\n📌 [{sample['법령구분']}] {sample['법령명']} ({sample['텍스트길이']:,}자, {sample['조문수']}조)")
        print(f"   처음 300자:")
        print(f"   {sample['전문'][:300]}...")
        break

# %% [8] 저장
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

file_size = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
print(f"\n💾 저장 완료: {OUTPUT_FILE} ({file_size:.1f}MB)")
print(f"   다음 단계: 이 파일을 LLM에 넣어 법령별 요약 생성")