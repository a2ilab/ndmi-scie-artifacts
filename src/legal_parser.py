import os
import re
from bs4 import BeautifulSoup
from langchain_core.documents import Document

class LegalParser:
    """
    법령 HTML의 계층 구조를 분석하여 RAG에 최적화된 청크(Chunk)로 분할하는 파서입니다.
    단순한 텍스트 분할이 아닌, 법령의 '조(Article)'와 '항(Clause)' 단위를 유지하며 메타데이터를 추출합니다.
    """
    def __init__(self):
        # 정규식 설명: 제N조 또는 제N조의M 형태와 그 옆의 (제목)을 추출
        self.ARTICLE_HEADER_RE = re.compile(r"^\s*제(\d+(?:조의\d+)?)조\s*\(([^)]+)\)")

    def _get_text(self, el) -> str:
        """BeautifulSoup 객체에서 공백을 정규화하여 순수 텍스트만 추출합니다."""
        return el.get_text(" ", strip=True) if el else ""

    def parse_optimized_legal_chunks(self, html: str, file_name: str) -> list[Document]:
        """
        HTML 태그를 분석하여 계층적 메타데이터를 포함한 Document 리스트를 반환합니다.
        
        Args:
            html: 법령 원문 HTML 문자열
            file_name: 소스 파일명 (메타데이터용)
            
        Returns:
            List[Document]: 법령 조항 단위로 분할된 문서 객체들
        """
        # [HTML 클래스 규칙]
        # pty1: 조 제목 / pty1_de2_1: 항 / pty1_de2h: 호 / pty1_de3h: 목
        CLAUSE_CLASS = "pty1_de2_1" 
        ITEM_CLASS = "pty1_de2h"     
        SUB_ITEM_CLASS = "pty1_de3h"  

        soup = BeautifulSoup(html, "lxml")
        
        # 법령 명칭 추출 (HTML 상단 중앙 h2 태그)
        h2 = soup.select_one("div.confnla1 > h2")
        law_name = self._get_text(h2)
        file_basename = os.path.basename(file_name)
        
        pgroups = soup.find_all("div", class_="pgroup")
        docs = []
        i = 0
        
        while i < len(pgroups):
            pg = pgroups[i]
            p_art = pg.find("p", class_="pty1") # '조' 시작 지점 탐색
            if not p_art:
                i += 1; continue
                
            full_art_text = self._get_text(p_art)
            m = self.ARTICLE_HEADER_RE.search(full_art_text)
            if not m:
                i += 1; continue
            
            # 메타데이터 추출: 조 번호(10의2 등) 및 조 제목    
            art_num, art_title = m.group(1), m.group(2).strip()
            article_prefix = f"[{law_name} 제{art_num}조({art_title})]"
            
            # 조 번호를 제외한 본문 시작 텍스트(Lead-in text) 확보
            art_body_start = self.ARTICLE_HEADER_RE.sub("", full_art_text).strip()
            lead_in_text = art_body_start
            
            # 동일 '조'에 속한 하위 태그들을 수집 (다음 조가 나타날 때까지)
            current_article_tags = pg.find_all("p")
            j = i + 1
            while j < len(pgroups):
                next_pg = pgroups[j]
                # pty1(새로운 조) 또는 pty3_dep1(장/절 구분)이 나오면 루프 종료
                if next_pg.find("p", class_="pty1") or next_pg.find("p", class_="pty3_dep1"):
                    break
                current_article_tags.extend(next_pg.find_all("p"))
                j += 1
            
            # '항(Clause)' 단위로 데이터 그룹화 수행    
            clauses = []
            current_clause = None
            current_item = None
            first_list_found = False

            for p in current_article_tags:
                if p is p_art: continue 
                txt = self._get_text(p)
                if not txt: continue
                cls = " ".join(p.get("class", []))
                
                # 현재 태그가 '항/호/목' 중 하나인지 확인
                is_list = any(c in cls for c in [CLAUSE_CLASS, ITEM_CLASS, SUB_ITEM_CLASS])
                
                # 리스트(항/호/목)가 나오기 전의 텍스트는 조문의 공통 서술(Lead-in)로 간주
                if not first_list_found and not is_list:
                    lead_in_text += "\n" + txt
                    continue
                elif is_list:
                    first_list_found = True
                
                # [계층 구조 파싱 로직]
                if CLAUSE_CLASS in cls: # 항(Clause) 탐색
                    current_clause = {"text": txt, "items": []}
                    clauses.append(current_clause)
                    current_item = None
                elif ITEM_CLASS in cls: # 호(Item) 탐색
                    current_item = {"text": txt, "sub_items": []}
                    if not current_clause:
                        current_clause = {"text": None, "items": []}
                        clauses.append(current_clause)
                    current_clause["items"].append(current_item)
                elif SUB_ITEM_CLASS in cls: # 목(Sub-item) 탐색
                    if current_item:
                        current_item["sub_items"].append(txt)

            # [최종 텍스트 구성 및 메타데이터 주입]
            if clauses:
                for idx, cl in enumerate(clauses, 1):
                    # 하위 '호'와 '목'을 텍스트로 병합 (LLM 컨텍스트 전달용)
                    merged_items = ""
                    for item in cl["items"]:
                        sub_str = " ".join([f"({s})" for s in item["sub_items"]]) if item["sub_items"] else ""
                        merged_items += f"\n  - {item['text']} {sub_str}"
                    
                    clause_main = cl["text"] if cl["text"] else ""
                    # [법령명 제N조] + 조문 서두 + 항 내용 + 호/목 리스트 형태로 결합
                    full_content = f"{article_prefix}\n{lead_in_text.strip()}\n{clause_main}{merged_items}".strip()
                    
                    meta = {
                        "law_name": law_name, # 법령명
                        "source_file": file_basename, # 출처 파일명
                        "chunk_id": f"{file_basename}_제{art_num}조_항{idx}", # 고유 키
                        "article_number": art_num, # 조 번호
                        "article_title": art_title, # 조 제목
                        "clause_number": idx # 항 번호
                    }
                    docs.append(Document(page_content=full_content, metadata=meta))
            else:
                # 항 구분 없이 조문만 있는 경우의 예외 처리
                full_content = f"{article_prefix}\n{lead_in_text.strip()}".strip()
                meta = {
                    "law_name": law_name,
                    "source_file": file_basename,
                    "chunk_id": f"{file_basename}_제{art_num}조",
                    "article_number": art_num,
                    "article_title": art_title,
                    "clause_number": None
                }
                docs.append(Document(page_content=full_content, metadata=meta))
            i = j 
        return docs