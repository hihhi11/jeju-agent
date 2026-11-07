"""터미널에서 에이전트에게 묻는 스크립트. 유료 모델을 고르면 비용이 든다.

    python scripts/ask.py "2024년 제주 입도객은 전부 몇 명이야?"
    python scripts/ask.py          # 이어서 묻기. 빈 줄을 입력하면 끝난다
    python scripts/ask.py "넣은 카드 자료로 업종별 이용금액" --center data/center_sample/가상_1-08_카드이용현황읍면동_202407.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import Agent, AgentAnswer
from agent.center import CenterStore
from agent.llm import LLMError


def show(answer: AgentAnswer) -> None:
    print(answer.text)
    if answer.unverified:
        print(f"\n[주의] 도구 결과에서 확인되지 않은 숫자: {', '.join(answer.unverified)}")
    calls = ", ".join(f"{c['name']}({c['status']})" for c in answer.tool_calls)
    print(f"\n[도구] {calls or '없음'}  [토큰] {answer.usage}")


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?")
    parser.add_argument("--center", nargs="+", default=[], help="화면의 'CSV 넣기' 처럼 함께 넣을 센터 CSV 파일")
    args = parser.parse_args()

    store = CenterStore()
    for path in map(Path, args.center):
        try:
            store.load(path.name, path.read_bytes())
        except ValueError as exc:
            print(exc)
            return 1
    try:
        agent = Agent(center_store=store)
    except LLMError as exc:
        print(exc)
        return 1
    if args.question:
        answer = agent.ask(args.question)
        show(answer)
        return 0 if answer.status == "ok" else 1
    while question := input("\n질문> ").strip():
        show(agent.ask(question))
    return 0


if __name__ == "__main__":
    sys.exit(main())
