#!/usr/bin/env python3
"""Synchronize the local current-law DB with the National Law DRF API.

The script keeps the existing folder structure under:
  outputs/collected_laws/법령

It adds missing laws and refreshes changed laws based on the official
lawSearch list. Existing unrelated files such as rag_enhanced.json are left
untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from paths import path_setting, output_path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(output_path("collected_laws/.root")).parent
LAW_ROOT = ROOT / "법령"
INDEX_DIR = ROOT / "indexes"
REPORT_DIR = ROOT / "sync_reports"
SOURCE_FILE_NAME = "원문_데이터.json"

BASE_URL = "http://www.law.go.kr/DRF"
DEFAULT_OC = os.environ.get("LAW_API_OC", "")

PRIMARY_TYPES = {"법률", "대통령령", "총리령"}


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def request(session: requests.Session, endpoint: str, params: dict[str, Any], timeout: int = 40) -> requests.Response:
    url = f"{BASE_URL}/{endpoint}"
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response


def request_json(session: requests.Session, endpoint: str, params: dict[str, Any], timeout: int = 40) -> dict[str, Any]:
    response = request(session, endpoint, params, timeout=timeout)
    return response.json()


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def remove_oc_credentials(value: Any) -> Any:
    """API 응답 URL의 OC 인증 파라미터를 저장 전에 재귀적으로 제거한다."""
    if isinstance(value, dict):
        return {key: remove_oc_credentials(item) for key, item in value.items()}
    if isinstance(value, list):
        return [remove_oc_credentials(item) for item in value]
    if not isinstance(value, str) or "oc=" not in value.lower():
        return value
    parts = urlsplit(value)
    if not parts.query:
        return value
    query = parse_qsl(parts.query, keep_blank_values=True)
    filtered = [(key, item) for key, item in query if key.lower() != "oc"]
    if len(filtered) == len(query):
        return value
    return urlunsplit(parts._replace(query=urlencode(filtered)))


def safe_name(value: Any, fallback: str = "untitled", limit: int = 110) -> str:
    text = str(value or fallback).strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[\\/:\*\?\"<>\|\x00-\x1f]", "_", text)
    text = text.strip("._ ")
    return (text or fallback)[:limit].rstrip("._ ")


def top_category(law_type: str) -> str:
    return law_type if law_type in PRIMARY_TYPES else "부령"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_official_law_list(session: requests.Session, oc: str, delay: float) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    page = 1
    display = 100
    total = None

    while True:
        data = request_json(
            session,
            "lawSearch.do",
            {
                "OC": oc,
                "target": "law",
                "type": "JSON",
                "query": "*",
                "display": display,
                "page": page,
                "sort": "lasc",
            },
        )
        root = data.get("LawSearch") or data.get("lawSearch") or data
        if total is None:
            total = int(root.get("totalCnt") or 0)
            print(f"official laws: {total:,}")

        items = as_list(root.get("law"))
        if not items:
            break

        all_items.extend(item for item in items if isinstance(item, dict))
        print(f"  page {page}: {len(items):,} items, collected {len(all_items):,}/{total:,}")

        if len(all_items) >= total:
            break
        page += 1
        time.sleep(delay)

    return all_items


def build_local_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for service_path in sorted(LAW_ROOT.glob(f"*/*/{SOURCE_FILE_NAME}")):
        folder = service_path.parent
        law_type_dir = folder.parent.name
        law_id = folder.name.split("__", 1)[0]
        service_info: dict[str, Any] = {}
        try:
            raw = read_json(service_path)
            service_info = (raw.get("법령") or {}).get("기본정보") or {}
            law_id = str(service_info.get("법령ID") or law_id).zfill(6)
        except Exception:
            pass
        index[law_id] = {
            "path": folder,
            "service_path": service_path,
            "type_dir": law_type_dir,
            "service_info": service_info,
        }
    return index


def existing_needs_refresh(local: dict[str, Any] | None, official: dict[str, Any], force: bool) -> bool:
    if force or local is None:
        return True
    service_path: Path = local["service_path"]
    if not service_path.exists():
        return True
    service_info = local.get("service_info") or {}
    checks = [
        ("법령일련번호", "법령일련번호"),
        ("시행일자", "시행일자"),
        ("공포일자", "공포일자"),
        ("공포번호", "공포번호"),
    ]
    return any(str(service_info.get(a) or "") != str(official.get(b) or "") for a, b in checks)


def target_dir_for(official: dict[str, Any], local: dict[str, Any] | None) -> Path:
    if local is not None:
        return local["path"]
    law_id = str(official.get("법령ID") or "").zfill(6)
    law_name = safe_name(official.get("법령명한글") or law_id)
    return LAW_ROOT / top_category(str(official.get("법령구분명") or "")) / f"{law_id}__{law_name}"


def collect_law_files(
    session: requests.Session,
    oc: str,
    official: dict[str, Any],
    save_dir: Path,
    include_secondary_formats: bool,
    delay: float,
) -> None:
    mst = official.get("법령일련번호") or ""
    ef_yd = official.get("시행일자") or ""
    if not mst:
        raise ValueError("missing 법령일련번호")

    base_params = {"OC": oc, "target": "law", "MST": mst, "efYd": ef_yd}

    data_json = remove_oc_credentials(
        request_json(session, "lawService.do", {**base_params, "type": "JSON"}, timeout=60)
    )
    write_json(save_dir / SOURCE_FILE_NAME, data_json)
    time.sleep(delay)

    if not include_secondary_formats:
        return

    data_xml = request(session, "lawService.do", {**base_params, "type": "XML"}, timeout=60)
    write_bytes(save_dir / "service.xml", data_xml.content)
    time.sleep(delay)

    data_html = request(session, "lawService.do", {**base_params, "type": "HTML", "mobileYn": ""}, timeout=60)
    write_bytes(save_dir / "print.html", data_html.content)
    time.sleep(delay)


def write_reports(
    stamp: str,
    official_items: list[dict[str, Any]],
    local_before: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    official_ids = {str(item.get("법령ID") or "").zfill(6) for item in official_items}
    local_ids = set(local_before.keys())
    official_counts = Counter(str(item.get("법령구분명") or "") for item in official_items)
    action_counts = Counter(str(item.get("action") or "") for item in actions)

    report = {
        "checked_at": now_iso(),
        "official_total": len(official_items),
        "local_total_before": len(local_before),
        "official_type_counts": dict(sorted(official_counts.items())),
        "missing_before_count": len(official_ids - local_ids),
        "extra_before_count": len(local_ids - official_ids),
        "missing_before_ids": sorted(official_ids - local_ids),
        "extra_before_ids": sorted(local_ids - official_ids),
        "action_counts": dict(sorted(action_counts.items())),
        "actions": actions,
        "errors": errors,
    }
    write_json(REPORT_DIR / f"law_sync_report_{stamp}.json", report)
    write_json(INDEX_DIR / "latest_law_official_list.json", official_items)

    lines = [
        "# 법령 DB 최신화 리포트",
        "",
        f"- 생성시각: `{report['checked_at']}`",
        f"- 공식 현행 법령: {len(official_items):,}건",
        f"- 로컬 동기화 전: {len(local_before):,}건",
        f"- 동기화 전 누락: {report['missing_before_count']:,}건",
        f"- 동기화 전 공식 현행 외 로컬: {report['extra_before_count']:,}건",
        f"- 오류: {len(errors):,}건",
        "",
        "## 작업 요약",
        "",
    ]
    for key, count in sorted(action_counts.items()):
        lines.append(f"- {key}: {count:,}건")
    write_text(REPORT_DIR / f"law_sync_report_{stamp}.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Current-law collection candidate; not the original corpus collector")
    parser.add_argument("--oc", default=DEFAULT_OC)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N official items after list fetch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Refresh every official law")
    parser.add_argument(
        "--include-secondary-formats",
        action="store_true",
        help="service.xml과 print.html도 함께 내려받음(기본값: 원문_데이터.json만 저장)",
    )
    args = parser.parse_args()

    if not args.oc:
        parser.error("Set LAW_API_OC or supply --oc before collecting")
    stamp = now_stamp()
    session = make_session()
    LAW_ROOT.mkdir(parents=True, exist_ok=True)

    official_items = fetch_official_law_list(session, args.oc, args.delay)
    local_before = build_local_index()

    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    items_to_process = official_items[: args.limit] if args.limit else official_items

    for idx, official in enumerate(items_to_process, 1):
        law_id = str(official.get("법령ID") or "").zfill(6)
        local = local_before.get(law_id)
        needs_refresh = existing_needs_refresh(local, official, args.force)
        save_dir = target_dir_for(official, local)
        action = "skip"
        if local is None:
            action = "add"
        elif needs_refresh:
            action = "refresh"

        if action == "skip":
            actions.append({"법령ID": law_id, "법령명": official.get("법령명한글"), "action": action, "path": str(save_dir)})
            continue

        print(f"[{idx:,}/{len(items_to_process):,}] {action} {law_id} {official.get('법령명한글')}")
        actions.append({"법령ID": law_id, "법령명": official.get("법령명한글"), "action": action, "path": str(save_dir)})
        if args.dry_run:
            continue

        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            collect_law_files(
                session,
                args.oc,
                official,
                save_dir,
                args.include_secondary_formats,
                args.delay,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"법령ID": law_id, "법령명": official.get("법령명한글"), "error": f"{type(exc).__name__}: {exc}"})
            print(f"  ERROR {law_id}: {exc}")

    write_reports(stamp, official_items, local_before, actions, errors)
    print(f"done: actions={Counter(a['action'] for a in actions)} errors={len(errors)}")
    print(f"reports: {REPORT_DIR}")


if __name__ == "__main__":
    main()
