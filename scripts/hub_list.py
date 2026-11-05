"""대회 페이지에서 받은 제주데이터허브 목록 파일을 직접 읽는 모듈.

    python scripts/hub_list.py      # 파일을 읽어 건수를 확인한다

목록 파일과 그 내용은 제3자 배포가 금지되어 있어 DB나 다른 파일로 옮기지 않고,
제출 zip이나 GitHub에 넣지 않는다. 실행하는 곳에서 파일을 data/raw/ 에 직접 넣는다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import openpyxl
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

SOURCE_PATTERN = "제주데이터허브*목록*.xlsx"
HEADER = ("No.", "카테고리", "데이터명", "데이터 타입", "등록일", "수정일", "수집 기간", "등록자", "조회수", "즐겨찾기수")
PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2}) ~ (?:(\d{4}-\d{2}-\d{2})|\(자동갱신: ([연월일])\))")


def _source_dir() -> Path:
    p = Path(os.getenv("DATA_RAW_DIR", "data/raw"))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _valid_date(value: str | None) -> str | None:
    # 수집 기간에는 0210-08-01 같은 값이 있어 비워 둔다. 원래 표기는 periodText 에 남는다
    return value if value and 1900 <= int(value[:4]) <= 2100 else None


def load_hub_list() -> list[dict] | None:
    """목록을 읽는다. 파일이 없으면 None 을 돌려주고, 여러 개면 파일 이름이 가장 뒤인 것을 쓴다."""
    found = sorted(_source_dir().glob(SOURCE_PATTERN))
    if not found:
        return None
    book = openpyxl.load_workbook(found[-1], read_only=True, data_only=True)
    try:
        rows = list(book.worksheets[0].iter_rows(values_only=True))
    finally:
        book.close()
    header_at = next((i for i, row in enumerate(rows) if row[0] == HEADER[0]), None)
    if header_at is None or tuple(rows[header_at]) != HEADER:
        raise ValueError(f"목록 파일의 컬럼이 예상과 다릅니다: {found[-1].name}")

    parsed = []
    for no, category, title, data_type, registered, updated, period, owner, views, favorites in rows[header_at + 1 :]:
        match = PERIOD.fullmatch(period)
        if not match:
            raise ValueError(f"{no}번 수집 기간을 읽지 못했습니다: {period}")
        start, end, auto_update = match.groups()
        types = data_type.split(",")
        parsed.append(
            {
                "listNo": no,
                "title": title.strip(),
                "category": category,
                "owner": owner,
                "hasApi": "API" in types,
                "hasFile": "File" in types,
                "hasLink": "Link" in types,
                "registeredDate": registered,
                "updatedDate": updated,
                "startDate": _valid_date(start),
                "endDate": _valid_date(end),
                "autoUpdate": auto_update,
                "periodText": period,
                "viewCount": int(views),
                "favoriteCount": int(favorites),
            }
        )
    return parsed


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    rows = load_hub_list()
    if rows is None:
        print(f"{_source_dir()} 에 목록 파일이 없습니다. 대회 페이지 데이터 탭에서 받아 넣어 주세요")
        return 1
    print(f"허브 목록: {len(rows):,}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
