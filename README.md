# NDMI SCIE 법령 RAG 연구자료

국립재난안전연구원(NDMI) 과제의 한국어 법령 RAG 연구 및 SCIE 논문 자료입니다. 법령 청킹·2단계 검색 코드, 평가셋 ID, 요약 생성 프롬프트와 결과, 용어 사전·관계 그래프를 제공합니다.

## 파일 구성

| 항목 | 주요 파일·폴더 | 포함 자료 |
| --- | --- | --- |
| 1. 청킹·2단계 검색 | `src/chunk_html.py`, `src/eval_pipeline_compare.py` | HTML 계층 청킹, 요약 검색·그래프 확장·청크 재검색 |
| 2. 법령 ID·수집 코드 | `data/corpus/law_ids.csv`, `src/collect_laws_candidate.py` | 법령 ID 5,556개, 법령 수집 코드 |
| 3. 평가셋 E2 | `data/evaluation/` | 평가 데이터 200건과 ID 목록 |
| 4. 프롬프트·요약 생성 결과 | `prompts/`, `data/summaries/` | 시스템·사용자 프롬프트, JSON 예시, 생성 결과 5,556레코드 |
| 5. 용어 사전·관계 그래프 | `data/terms/`, `data/graphs/` | 용어 연결 750,000개, 법령 관계 27,080개, API·LLM 출처 |

`law_summaries_all.json`은 전체 생성 결과 5,556레코드이며, `dictionary_input_5170.json`은 용어 사전 구성에 대응하는 5,170레코드 추출본입니다.

`term_edges.csv.gz`의 `source`는 `API`·`LLM`·`API|LLM`으로 구분합니다. `api_relation_forward`·`api_relation_reverse`는 원자료의 정방향·역방향 관계 라벨이고, `llm_mapping_kind`는 용어 연결 경로입니다. `law_edges.csv`에는 법령 관계와 출처가 함께 기록되어 있습니다.

## 사용법

자료 검증은 Python 3.10 이상 표준 라이브러리로 실행할 수 있습니다.

```bash
python3 tools/verify_release.py
python3 -m unittest discover -s tests -v
```

검색·수집·생성 코드의 패키지 목록은 [requirements.txt](requirements.txt)에 있습니다. `.env.example`을 `.env`로 복사한 뒤, 사용할 HTML·전문·FAISS 인덱스 경로와 API 설정을 입력합니다.

```bash
set -a
source .env
set +a
python3 tools/check_inputs.py
```

새 출력은 기본적으로 `outputs/`에 저장됩니다.

| 실행 파일 | 역할·추가 입력 |
| --- | --- |
| `src/chunk_html.py` | HTML 청킹. `SCIE_LAW_HTML` 필요. |
| `src/eval_pipeline_compare.py` | 청크 인덱스와 v3 요약 인덱스로 2단계 검색 평가. |
| `src/eval_ablation_study.py` | 청크 인덱스와 v2 요약 인덱스로 절제실험. |
| `src/collect_laws_candidate.py` | 법령 수집. `LAW_API_OC` 필요. |
| `src/collect_terms.py` | 용어·관계 API 수집. `LAW_API_OC` 필요. |
| `src/extract_fulltext.py` | HTML에서 전문을 추출해 `outputs/`에 저장. |
| `src/generate_summaries.py` | 전문을 받아 요약 생성. `SCIE_FULLTEXT_FILE`, Ollama 필요. |
| `src/build_graph.py` | 사전·그래프 구성. 기본 입력은 5,170건 추출본. |
| `tools/export_provenance.py` | 출처가 표시된 관계 CSV 내보내기. |

## 출처·검증 기록

파일 출처·변환 내역은 [source_manifest.json](provenance/source_manifest.json), 자료 검증 결과는 [validation.json](provenance/validation.json)에 기록되어 있습니다.
