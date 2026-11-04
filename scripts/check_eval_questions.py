"""시험 문제(reference/eval_questions.json)의 기대값을 로컬 DB와 대조하는 스크립트.

    python scripts/check_eval_questions.py

기대값이 DB와 다르면 데이터가 바뀐 것이므로 문제를 다시 확인한다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _db_path() -> Path:
    p = Path(os.getenv("DATA_DB_PATH", "data/jeju.duckdb"))
    return p if p.is_absolute() else PROJECT_ROOT / p


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    questions = json.loads((PROJECT_ROOT / "reference" / "eval_questions.json").read_text(encoding="utf-8"))["questions"]
    con = duckdb.connect(str(_db_path()), read_only=True)
    failed = total = 0
    for q in questions:
        for check in q["checks"]:
            total += 1
            actual = con.execute(check["sql"]).fetchone()[0]
            if actual != check["expected"]:
                failed += 1
                print(f"다름 {q['id']} {check['label']}: DB {actual!r}, 기대 {check['expected']!r}")
    con.close()
    print(f"문제 {len(questions)}개, 확인 {total}건, 다름 {failed}건")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
