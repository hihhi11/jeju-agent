"""질문 30개를 지금 답할 수 있는지 모델 없이 도구를 돌려 확인하는 스크립트. 비용이 들지 않는다.

    python scripts/check_coverage.py

표시와 도구 결과가 어긋나면 알린다. 도구가 값을 돌려줘도 그 질문의 답이 되는지는 사람이 본다.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent.common import clean
from agent.registry import execute_tool

# 값을 돌려주는 결과. not_comparable 은 이유와 대체 지표를 안내하므로 일부로 본다
ANSWERED_STATUSES = {"ok"}
PARTIAL_STATUSES = {"ok", "not_comparable", "center_only"}


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    questions = json.loads((PROJECT_ROOT / "reference" / "coverage_questions.json").read_text(encoding="utf-8"))["questions"]
    mismatched = 0
    for q in questions:
        check = q.get("check")
        status, first = "-", ""
        if check:
            result = execute_tool(check["tool"], check["args"])
            status = result.status
            has_values = bool(result.rows or result.summary.get("valueA"))
            first = result.message or json.dumps(clean(result.rows[:1]), ensure_ascii=False)
            problem = (q["coverage"] == "가능" and not (status in ANSWERED_STATUSES and has_values)) or (q["coverage"] == "일부" and status not in PARTIAL_STATUSES)
            if problem:
                mismatched += 1
                print(f"어긋남 {q['id']}: {q['coverage']} 인데 {status}")
        print(f"{q['id']} {q['coverage']} {status} {first[:160]}")

    counts = collections.Counter(q["coverage"] for q in questions)
    print(f"\n질문 {len(questions)}개: " + ", ".join(f"{k} {counts[k]}" for k in ("가능", "일부", "불가")))
    for user in dict.fromkeys(q["user"] for q in questions):
        part = [q for q in questions if q["user"] == user]
        print(f"  {user}: 가능 {sum(q['coverage'] == '가능' for q in part)}/{len(part)}")
    topics = collections.Counter(q["topic"] for q in questions if q["coverage"] != "가능")
    print("빈틈: " + ", ".join(f"{k} {v}" for k, v in topics.most_common()))
    print("근거: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(q["basis"] for q in questions).most_common()))
    print(f"어긋남 {mismatched}건")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
