# SCIE 공개용 자료 — 로컬 준비본

보존된 연구 자료를 모은 로컬 저장소입니다. GitHub에는 아직 업로드하지 않았습니다. 원본 데이터와 미확인 사항을 유지한 상태로 실행 설정·ID 목록·관계별 출처를 정리했습니다.

| 요청 자료 | 위치 | 준비 상태 |
| --- | --- | --- |
| 청킹·2단계 검색 코드 | `src/` | 계층 청킹 후보와 2단계 검색 코드 포함. 당시 청킹 코드와 실험 인덱스의 연결은 미확인. |
| 법령 ID·수집 코드 | `data/corpus/`, `src/collect_laws_candidate.py` | 고유 ID 5,556개. 수집 코드는 후대 후보이며 최초 수집 코드로 확인된 것은 아님. |
| E1·E2 선별 ID | `data/evaluation/` | E2 200개 ID와 평가 파일 포함. E1 49개 ID와 최초 선별 규칙은 미확인. |
| 프롬프트·요약본 | `prompts/`, `data/summaries/` | 프롬프트 전문과 요약 5,556레코드. 실패 표시 5개·별도 필드 누락 1개를 그대로 보존. |
| 용어 사전·관계 그래프 | `data/terms/`, `data/graphs/` | 보존 JSON과 API·LLM 출처를 표시한 CSV 포함. |

## 원본과 실행용 코드

- `data/`의 보존 JSON과 `prompts/`는 원본을 그대로 복사했습니다. API 용어 원자료만 원본 바이트를 유지하는 gzip으로 압축했습니다. 변환 여부와 SHA-256은 `provenance/source_manifest.json`에 있습니다.
- `src/`는 실행 경로·API 설정을 분리한 코드입니다. 2단계 검색·절제실험의 모든 함수, 청킹 파서, 요약 생성·파싱·재시도 함수는 보존본과 같습니다. 새 `chunk_html.py`는 파서를 호출해 JSONL로 저장하는 실행 도구입니다.
- 수정 전 코드와 청킹 노트북은 `.local/originals/`에 따로 보관했습니다. 이 로컬 보관 폴더와 현재 서버 경로를 담은 `.env.local`은 Git에서 제외됩니다.
- HTML 파일은 5,557개였지만 내용이 같은 중복 1개가 있어 고유 ID는 5,556개입니다. 이 수량을 논문 당시 코퍼스의 확정 수량으로 대체하지 않습니다.
- `summary_issues.csv`에 문제 레코드 6개를 표시했습니다. 실패 기록이나 `검색용_요청문` 키를 임의 수정하거나, 새 요약을 만들어 채우지 않았습니다.

## 출처 열 읽는 방법

`data/terms/term_edges.csv.gz`는 750,000개의 유향 용어 연결입니다. `source`는 `API`·`LLM`·`API|LLM`, `from_api`와 `from_llm`은 각각 0 또는 1입니다.

| 출처 | 연결 수 |
| --- | ---: |
| API만 | 646,469 |
| LLM만 | 98,204 |
| API·LLM 공통 | 5,327 |

API의 관계 라벨은 `api_relation_forward`와 `api_relation_reverse`에 나누어 보존했습니다. 전자는 현재 연결 방향으로 원자료에 기록된 유형, 후자는 반대 방향으로 기록된 유형입니다. 예를 들어 역방향의 ‘상위어’를 임의로 ‘하위어’로 바꾸지 않았습니다. `llm_mapping_kind`는 법령용어↔일상용어·일상용어↔일상용어의 연결 경로이며, 동의어·연관어·상위어·하위어 분류가 아닙니다. LLM의 해당 의미 관계 유형은 미확인입니다.

출처 재구성에는 보존 사전과 내용이 일치하는 `dictionary_input_5170.json`을 사용했습니다. 이 파일은 보존 v2 인덱스 ID를 기준으로 기존 요약에서 추출한 자료이며, 실험 당시 JSON 원본은 아닙니다. 5,556건 전체를 넣으면 다른 사전이 됩니다.

`data/graphs/law_edges.csv`는 법령 간 관계 27,080개이며, 보존 그래프의 출처를 그대로 옮겼습니다(API 744개, LLM 26,336개). 원래 그래프가 선택한 출처이며, 저장되지 않은 중복 출처를 새로 추정하지 않았습니다.

## 확인과 실행 설정

저장소 루트에서 다음 명령은 모델·검색·API 호출 없이 자료를 확인합니다. Python 3.10 이상 표준 라이브러리만 필요합니다.

```bash
python3 tools/verify_release.py
python3 -m unittest discover -s tests -v
python3 tools/check_inputs.py
```

현재 서버의 보존 인덱스·HTML·본문 경로는 `.env.local`에 연결해 두었습니다. 이 파일은 자동으로 읽지 않으며, 적용하려면 다음을 실행합니다.

```bash
set -a
source .env.local
set +a
python3 tools/check_inputs.py
```

다른 환경에서는 `.env.example`을 참고해 경로를 지정합니다. HTML·전문·대용량 FAISS 인덱스는 이 Git 저장소에 포함하지 않았습니다. 새 출력의 기본 위치는 `outputs/`입니다. `LAW_API_OC`는 비워 두었으며 실제 수집 시 사용자 설정이 필요합니다.

| 실행 파일 | 역할·추가 입력 |
| --- | --- |
| `src/chunk_html.py` | 보존 후보 파서로 HTML을 청킹. `SCIE_LAW_HTML` 필요. |
| `src/eval_pipeline_compare.py` | 보존 청크 인덱스와 v3 요약 인덱스로 2단계 검색 평가. |
| `src/eval_ablation_study.py` | 보존 청크 인덱스와 v2 요약 인덱스로 절제실험. |
| `src/collect_laws_candidate.py` | 후대 법령 수집 코드 후보. `LAW_API_OC` 필요. |
| `src/collect_terms.py` | 용어·관계 API 수집. `LAW_API_OC` 필요. |
| `src/extract_fulltext.py` | HTML에서 전문을 추출해 `outputs/`에 저장. |
| `src/generate_summaries.py` | 전문을 받아 요약 생성. `SCIE_FULLTEXT_FILE`, Ollama 필요. |
| `src/build_graph.py` | 보존 구성 로직으로 별도 출력 생성. 기본 입력은 5,170건 추출본. |
| `tools/export_provenance.py` | 보존 자료에서 출처 CSV만 다시 내보냄. 모델·검색 실행 없음. |

`requirements.txt`는 정리 작업에 사용한 Python 3.13.11 환경의 패키지 버전이며, 실험 당시 환경을 복구한 잠금 파일은 아닙니다. 이번 정리에서는 자료·코드·설정 검증만 수행했으며, 검색 성능 평가·인덱스 재생성·요약 생성·API 재수집은 실행하지 않았습니다.

## 아직 필요한 자료

E1 49개 ID, E1·E2 최초 선별 규칙, 당시 코퍼스 최초 수집 코드, 당시 청킹 실행과 인덱스를 연결하는 근거, 완성 요약본 5,557건의 근거는 미확인입니다. 이 저장소는 공개 준비본이며, 현재 논문의 모든 실험을 독립적으로 재현할 수 있다고 확정한 상태는 아닙니다.
