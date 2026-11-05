"""에이전트 루프. 질문을 LLM에 보내고, 도구를 실행해 돌려주고, 답변의 숫자를 대조한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agent.common import ToolResult
from agent.llm import Conversation, LLMError, ToolOutcome, Turn, make_conversation
from agent.metrics import metrics_for_prompt
from agent.registry import TOOL_DEFINITIONS, execute_tool
from agent.verify import unverified_numbers

# 한 질문에서 LLM을 부르는 횟수의 상한. 시험 문제는 도구 2~3번이면 답이 나온다
MAX_STEPS = 8
ERROR_STATUSES = {"invalid_request", "error"}

SYSTEM_PROMPT = f"""당신은 제주 관광 데이터 분석 도우미입니다. 도민, 소상공인, 공무원이 일상어로 물으면 도구로 계산한 결과를 바탕으로 쉬운 한국어로 답합니다. 사용자는 IT 비전문가입니다. 표 이름, 컬럼 이름, 지표 ID, 도구 이름을 답변에 쓰지 않습니다.

## 날짜와 자료 기간
오늘은 {date.today():%Y-%m-%d}입니다. 어떤 자료가 어느 기간까지 있는지는 당신이 알 수 없고 도구 결과로만 알 수 있습니다. 질문의 시점이 미래처럼 보여도 도구를 불러 확인합니다. 데이터에 관한 질문은 도구를 부르지 않고 답하지 않습니다. 도구 결과에 없는 연도나 기간을 말하지 않습니다.

## 숫자
수치는 모두 도구 결과에서 그대로 옮깁니다. 직접 더하거나 나누거나 추정하지 않습니다. 필요한 값이 결과에 없으면 다른 조건으로 도구를 다시 부르거나 알 수 없다고 말합니다. 시스템이 답변의 숫자를 도구 결과와 대조합니다. 자료가 없는 기간은 0이 아니라 자료 없음입니다.

## 도구 결과의 status
- ok: 결과로 답합니다.
- needs_clarification: 후보를 보여 주고 사용자에게 되묻습니다. 임의로 고르지 않습니다.
- out_of_range: 요청 기간에 자료가 없다고 먼저 말하고, 제공 기간과 가장 가까운 값을 그 해의 값으로 안내합니다.
- not_comparable: 비교할 수 없는 이유와 제공 기간을 답변 맨 앞에 말합니다. alternatives 가 있으면 그 지표로 도구를 다시 불러 함께 안내하되, 그 값을 원래 지표의 이름으로 부르지 않습니다.
- insufficient_sample: 표본 수를 말하고 답을 유보합니다.
- center_only: 명세만 있어 값을 계산할 수 없다고 말합니다. describe_data 로 가장 관련 있는 명세 하나의 항목을 확인해 안내합니다. 금액 등을 지어내지 않습니다.
- not_found, invalid_request: 입력을 고쳐 다시 부르거나 없다고 말합니다.

## 지표
{metrics_for_prompt()}

입도객 전체 수는 arrivals 만 씁니다. 사용자가 틀린 계산 방법을 말하면 그대로 따르지 않고 왜 안 되는지 결과의 수치로 설명합니다 (예: 두 시를 더해 제주 전체로 보기). 최근 동향을 물으면 일부 달만 있는 해를 비교하지 않고 summary.recent12Months 를 씁니다. 순위 기준이 여럿이면 어떤 기준의 순위인지 밝힙니다.

## 답변 형식
바로 답을 말한 뒤 해석을 3줄 안쪽으로 씁니다. 그 뒤에 다음 두 항목을 짧게 붙입니다.
**근거**: 쓴 데이터, 기간, 계산 방법 (evidence 에서 옮김)
**한계**: 결과의 limits 중 이 답을 읽을 때 꼭 알아야 할 것
상관관계를 인과로 단정하지 않습니다. "함께 나타난다"처럼 씁니다. 결과에 없는 원인이나 특성을 덧붙이지 않습니다. 데이터 목록을 안내할 때는 데이터마다 제공 기간을 함께 쓰고, 센터 전용 데이터는 값을 계산할 수 없다고 밝힙니다. 한국어로만 쓰고 한자나 외국어를 섞지 않습니다."""

CORRECTION = "답변의 숫자나 연도 중 도구 결과에서 찾지 못한 값이 있습니다: {numbers}. 도구 결과에 있는 값만 쓰도록 답변 전체를 다시 쓰세요. 직접 계산한 값이면 빼세요."
NO_TOOL_NUDGE = "도구를 부르지 않고 답했습니다. 자료의 유무와 기간은 도구 결과로만 알 수 있습니다. 데이터에 관한 질문이면 도구로 확인한 뒤 다시 답하세요. 인사처럼 데이터와 무관한 말이면 그대로 답해도 됩니다."
STEP_LIMIT_TEXT = "질문을 처리하는 단계가 너무 많아 멈췄습니다. 질문을 더 좁혀서 다시 물어봐 주세요."
TRUNCATED_TEXT = "답변이 너무 길어 끊겼습니다. 질문을 나눠서 다시 물어봐 주세요."
REFUSED_TEXT = "이 질문에는 답할 수 없습니다. 제주 관광 데이터에 관한 질문으로 다시 물어봐 주세요."


@dataclass
class AgentAnswer:
    text: str
    # ok, llm_error, refused, truncated, step_limit
    status: str = "ok"
    tool_calls: list[dict] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class Agent:
    """사용자 한 명과의 대화. 되묻기에 답할 수 있게 ask 사이에 기록을 유지한다."""

    def __init__(self, conversation: Conversation | None = None, max_steps: int = MAX_STEPS):
        self.conversation = conversation or make_conversation(SYSTEM_PROMPT, TOOL_DEFINITIONS)
        self.max_steps = max_steps
        self.question_log: list[str] = []
        self.all_results: list[ToolResult] = []

    def ask(self, question: str) -> AgentAnswer:
        self.question_log.append(question)
        answer = AgentAnswer(text="")
        try:
            turn = self._record(answer, self.conversation.send_user(question))
            corrected = nudged = False
            for _ in range(self.max_steps):
                if turn.stop == "tool_use":
                    turn = self._record(answer, self.conversation.send_tool_results(self._run_tools(turn, answer)))
                elif turn.stop == "done" and not self.all_results and not nudged:
                    # 작은 모델은 자료가 없다고 짐작하고 바로 답하는 일이 있어 한 번 되돌린다
                    nudged = True
                    turn = self._record(answer, self.conversation.send_user(NO_TOOL_NUDGE))
                elif turn.stop == "done":
                    missing = unverified_numbers(turn.text, self.all_results, " ".join(self.question_log))
                    if not missing or corrected:
                        answer.text, answer.unverified = turn.text, missing
                        return answer
                    corrected = True
                    turn = self._record(answer, self.conversation.send_user(CORRECTION.format(numbers=", ".join(missing))))
                else:
                    answer.text, answer.status = (REFUSED_TEXT, "refused") if turn.stop == "refused" else (TRUNCATED_TEXT, "truncated")
                    return answer
        except LLMError as exc:
            answer.text, answer.status = str(exc), "llm_error"
            return answer
        answer.text, answer.status = STEP_LIMIT_TEXT, "step_limit"
        return answer

    def _run_tools(self, turn: Turn, answer: AgentAnswer) -> list[ToolOutcome]:
        outcomes = []
        for call in turn.tool_calls:
            result = execute_tool(call.name, call.arguments)
            answer.tool_calls.append({"name": call.name, "arguments": call.arguments, "status": result.status})
            answer.results.append(result)
            self.all_results.append(result)
            outcomes.append(ToolOutcome(call.id, result.to_llm_json(), result.status in ERROR_STATUSES))
        return outcomes

    @staticmethod
    def _record(answer: AgentAnswer, turn: Turn) -> Turn:
        for key, value in turn.usage.items():
            answer.usage[key] = answer.usage.get(key, 0) + value
        return turn
