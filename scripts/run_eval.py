"""시험 문제를 실제 LLM으로 풀어 채점하는 스크립트. 유료 모델을 고르면 비용이 든다.

    python scripts/run_eval.py                  # 16문제 전부
    python scripts/run_eval.py --ids Q03,Q05    # 고른 문제만

자동으로 보는 것은 기대 도구 사용, 꼭 말할 내용, 확인되지 않은 숫자다. 한계 표시와 함정은 저장된 답변을 사람이 읽어 본다.
결과는 data/eval_runs/ 에 저장한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent.agent import Agent
from agent.common import clean
from agent.llm import LLMError, current_model


def _with_month_format(text: str) -> str:
    """'2021년 8월' 도 '2021-08' 과 같게 본다. 답변에 두 표기가 모두 남게 덧붙인다."""
    return text + " " + " ".join(f"{y}-{int(m):02d}" for y, m in re.findall(r"(\d{4})년 ?(\d{1,2})월", text))


def score(question: dict, answer) -> dict:
    used = [c["name"] for c in answer.tool_calls]
    missing_tools = [t for t in question["expectedTools"] if t not in used]
    text = _with_month_format(answer.text)
    missing_mentions = [m for m in question["mustMention"] if m not in text]
    passed = answer.status == "ok" and not missing_tools and not missing_mentions and not answer.unverified
    return {
        "id": question["id"],
        "question": question["question"],
        "passed": passed,
        "status": answer.status,
        "missingTools": missing_tools,
        "missingMentions": missing_mentions,
        "unverified": answer.unverified,
        "toolCalls": answer.tool_calls,
        "usage": answer.usage,
        "answer": answer.text,
        "expectedAnswer": question["expectedAnswer"],
        "expectedLimits": question["limits"],
        "trap": question.get("trap", ""),
    }


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", help="쉼표로 구분한 문제 ID")
    args = parser.parse_args()

    questions = json.loads((PROJECT_ROOT / "reference" / "eval_questions.json").read_text(encoding="utf-8"))["questions"]
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    provider, model = current_model()
    print(f"모델: {provider} {model}")
    reports = []
    totals: dict[str, int] = {}
    for q in questions:
        # 문제끼리 기록이 섞이지 않게 문제마다 새 대화로 푼다
        try:
            answer = Agent().ask(q["question"])
        except LLMError as exc:
            print(exc)
            return 1
        report = score(q, answer)
        reports.append(report)
        for key, value in answer.usage.items():
            totals[key] = totals.get(key, 0) + value
        reason = "; ".join(filter(None, [
            f"상태 {answer.status}" if answer.status != "ok" else "",
            f"도구 빠짐 {report['missingTools']}" if report["missingTools"] else "",
            f"내용 빠짐 {report['missingMentions']}" if report["missingMentions"] else "",
            f"미확인 숫자 {report['unverified']}" if report["unverified"] else "",
        ]))
        print(f"{'통과' if report['passed'] else '실패'} {q['id']} {reason}")
        if answer.status == "llm_error":
            # 한도에 걸려도 그때까지의 답변은 저장한다
            print(answer.text)
            break

    out_dir = PROJECT_ROOT / "data" / "eval_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(clean({"provider": provider, "model": model, "totals": totals, "reports": reports}), ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(r["passed"] for r in reports)
    print(f"통과 {passed}/{len(reports)}, 토큰 {totals}")
    print(f"답변 저장: {out_path}")
    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
