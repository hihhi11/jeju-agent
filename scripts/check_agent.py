"""LLM 없이 분석 도구와 루프를 확인하는 스크립트. API 키가 필요 없고 비용이 들지 않는다.

    python scripts/check_agent.py

도구 결과를 시험 문제의 기대값과 대조하고, 정해진 응답을 돌려주는 가짜 LLM과 가짜 서버로 루프와 Claude 호출 코드를 시험한다.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import anthropic

import agent.agent as agent_module
from agent.agent import SYSTEM_PROMPT, Agent, AgentAnswer
from agent.common import ToolResult, clean
from agent.display import DIMMED_OPACITY, build_figure, chart_note, clarification_choices, count_columns, csv_bytes, evidence_lines, hub_frame, rows_frame, safe_markdown, visible_results
from agent.llm import ClaudeConversation, LLMError, OpenAICompatibleConversation, ToolCall, Turn
from agent.registry import TOOL_DEFINITIONS, execute_tool
from agent.common import query
from agent.tools import analyze_weather_effect, compare_periods, describe_data, find_data, get_day_pattern, get_seasonality, get_trend, rank_values, resolve_spot
from agent.verify import unverified_numbers

_failed = 0
_passed = 0


def check(label: str, actual, expected) -> None:
    global _failed, _passed
    if actual == expected:
        _passed += 1
    else:
        _failed += 1
        print(f"다름 {label}: 결과 {actual!r}, 기대 {expected!r}")


def rows_of(result) -> list[dict]:
    return clean(result.rows)


def check_tools() -> None:
    q01 = find_data("관광")
    check("Q01 상태", q01.status, "ok")
    check("Q01 관광지 자료 끝", next(r["endDate"] for r in clean(q01.rows) if r["id"] == "spot_visitors_monthly"), "2024-12-01")

    q02 = find_data("관광객 카드 소비")
    check("Q02 센터 전용", q02.status, "center_only")
    check("Q02 명세 조회", describe_data("1-01").status, "center_only")
    check("Q02 반출 불가", describe_data("1-01").summary["exportable"], False)
    check("허브 목록은 LLM에 보내지 않음", "hubMatches" in q02.to_llm_json(), False)

    check("자료 찾기: 없는 값을 다른 자료로 답하지 않음", (find_data("렌터카 이용 비율").rows, find_data("렌터카 이용 비율").summary["tokensNotFound"]), ([], ["렌터카"]))
    check("자료 찾기: 흔한 말만 있으면 전체에서 찾음", len(find_data("관광").rows) > 0, True)
    check("자료 찾기: 흔한 말은 빼고 찾음", all("입도" not in r["title"] for r in find_data("관광객 버스").rows), True)
    check("자료 찾기: 일부 낱말이 없으면 알림", "크루즈" in find_data("버스 크루즈").message, True)

    # 기대값은 도구와 별개로 SQL로 계산한다
    expected = query(
        "SELECT round(avg(t.visitors) FILTER (WHERE NOT d.isOffDay)) AS workday, round(avg(t.visitors) FILTER (WHERE d.isWeekend)) AS weekend, "
        "count(*) FILTER (WHERE NOT d.isOffDay) AS workdays, count(*) FILTER (WHERE d.isWeekend) AS weekends "
        "FROM tour_metro_daily t JOIN dim_date d USING (baseDate) WHERE t.visitorClass = '외지인' AND t.baseDate BETWEEN DATE '2025-10-01' AND DATE '2026-09-30'"
    ).iloc[0]
    day = get_day_pattern("visitors_jeju", {"visitorClass": "외지인"}, "2025-10", "2026-09")
    check("요일: 평일·주말 평균", (day.summary["workdayAverage"], day.summary["weekendAverage"], day.summary["workdayDays"], day.summary["weekendDays"]), (expected["workday"], expected["weekend"], expected["workdays"], expected["weekends"]))
    check("요일: 기간을 안 주면 온전한 12개월", get_day_pattern("visitors_jeju", {"visitorClass": "외지인"}).evidence["period"], "2025-10~2026-09")
    check("요일: 요일 7개가 순서대로", [r["dayOfWeek"][0] for r in clean(day.rows)], list("월화수목금토일"))
    check("요일: 월별 지표는 거부", get_day_pattern("arrivals").status, "not_comparable")
    check("요일: 두 시를 더하지 않음", get_day_pattern("visitors_city", {"visitorClass": "외지인"}).status, "needs_clarification")
    check("요일: 없는 기간", get_day_pattern("visitors_jeju", {"visitorClass": "외지인"}, "2010", "2011").status, "out_of_range")
    check("요일: 날이 부족하면 말하지 않음", get_day_pattern("visitors_jeju", {"visitorClass": "외지인"}, "2025-10", "2025-10").status in ("ok", "insufficient_sample"), True)
    check("요일: 필수 필터 확인", get_day_pattern("visitors_jeju").status, "needs_clarification")

    q03 = rank_values("spot_visitors", "2024", "2024", top_n=5)
    check("Q03 순위", [(r["spot"], r["value"]) for r in rows_of(q03)], [("한라산국립공원", 928409), ("성산일출봉", 906188), ("천지연폭포", 890241), ("중문대포해안주상절리대", 816210), ("카멜리아힐", 774153)])
    check("Q03 합산 표시", q03.evidence["visitorScope"], "내국인+외국인 합산")

    q04 = get_trend("visit_purpose", {"purpose": "레저"})
    recent = q04.summary["recent12Months"]
    check("Q04 최근 12개월", (recent["recentValue"], recent["priorValue"], recent["changePct"]), (1276626, 1499257, -14.8))
    rows = {r["period"]: r for r in rows_of(q04)}
    check("Q04 연간", (rows["2023"]["value"], rows["2024"]["value"]), (1591635, 1321675))
    check("Q04 일부 달만 있는 해는 전년 비교 안 함", (rows["2021"]["isComplete"], rows["2025"]["yoyPct"]), (False, None))
    check("Q04 일부 달만 있는 해를 한계로 알림", any("2021, 2025년은 일부 달만" in t for t in q04.limits), True)
    check("visit_purpose 는 기준 없이 합산 안 함", get_trend("visit_purpose").status, "needs_clarification")

    q05 = rows_of(get_trend("arrivals", start="2024", end="2024"))[0]
    check("Q05 합계", (q05["value"], q05["breakdown"]["내국인"], q05["breakdown"]["외국인"]), (13767350, 11861654, 1905696))

    check("기간이 잘리면 대체 지표를 알림", get_trend("arrivals", start="2019", end="2024").alternatives[0]["metric"], "air_passengers")

    q06 = compare_periods("spot_visitors", "2019", "2019", "2024", "2024")
    check("Q06 곳 수", (q06.summary["comparableCount"], q06.summary["reachedBaseCount"]), (27, 6))
    check("Q06 회복률", [(r["spot"], r["ratioPct"]) for r in rows_of(q06)][:6], [("서복전시관", 323.1), ("제주도립 김창열미술관", 167.6), ("한라산국립공원", 109.4), ("제주돌문화공원", 109), ("카멜리아힐", 107.7), ("국립제주박물관", 100.2)])
    check("Q06 만장굴 제외", "만장굴관광지" in [e["name"] for e in q06.summary["excluded"]], True)

    q07 = compare_periods("arrivals", "2019", "2019", "2024", "2024")
    check("Q07 입도객 비교 불가", (q07.status, q07.alternatives[0]["metric"]), ("not_comparable", "air_passengers"))
    q07b = compare_periods("air_passengers", "2019", "2019", "2024", "2024", {"direction": "도착"})
    check("Q07 항공 지표", (q07b.summary["valueA"], q07b.summary["valueB"], q07b.summary["ratioPct"]), (15657540, 14798780, 94.5))
    check("항공 지표는 방향 필수", compare_periods("air_passengers", "2019", "2019", "2024", "2024").status, "needs_clarification")

    q08 = get_seasonality("spot_visitors", {"sigungu": "서귀포"})
    check("Q08 2024 상위 달", q08.summary["latestYearTopMonths"], [{"month": 5, "value": 707156}, {"month": 10, "value": 684359}])
    check("Q08 같은 관광지 평균", (q08.summary["averageBasisCount"], q08.summary["averagePeakMonth"]), (11, 10))

    q09 = analyze_weather_effect()
    check("Q09 강수 구간", [(r["range"], r["days"], r["changePct"]) for r in rows_of(q09)], [("1~10mm", 380, -1.9), ("10~30mm", 175, -4.9), ("30mm 이상", 103, -10.8)])

    check("Q10 성산", rows_of(resolve_spot("성산"))[0]["spotName"], "성산일출봉")
    q10 = get_trend("spot_visitors", {"spot": "성산"}, "2025", "2025")
    check("Q10 기간 밖", (q10.status, q10.summary["availablePeriod"], rows_of(q10)[0]["value"]), ("out_of_range", "2009-01~2024-12", 906188))

    q11 = rank_values("visitors_city", "2025", "2025", {"visitorClass": "외지인"})
    check("Q11 시별", [(r["sigungu"], r["value"]) for r in rows_of(q11)], [("제주시", 60111114), ("서귀포시", 44107350)])

    q12 = get_trend("visitors_jeju", {"visitorClass": "외지인"}, "2025", "2025")
    check("Q12 제주 전체", rows_of(q12)[0]["value"], 59269886)
    check("Q12 배율", q12.summary["cityComparison"]["twoCitySumToJejuRatio"], 1.76)
    check("Q12 배율을 한계로 알림", any("1.76배" in t for t in q12.limits), True)
    check("두 시를 합치지 않음", len(rows_of(get_trend("visitors_city", {"visitorClass": "외지인"}, "2025", "2025"))), 2)
    check("기간 밖에서도 두 시를 합치지 않음", [r["sigungu"] for r in rows_of(get_trend("visitors_city", {"visitorClass": "외국인"}, "2018", "2018"))], ["서귀포시", "제주시"])

    q13 = get_trend("visitors_city", {"visitorClass": "외국인", "sigungu": "제주시"}, "2018", "2018")
    check("Q13 기간 밖", (q13.status, q13.summary["nearestYear"], rows_of(q13)[0]["value"]), ("out_of_range", 2019, 4683754))
    check("Q13 제공 시작", q13.summary["availablePeriod"].split("~")[0], "2019-01")

    q14 = rank_values("bus_station", "2026-10", "2026-10", top_n=3)
    check("Q14 이름 기준", [(r["name"], r["value"]) for r in rows_of(q14)], [("제주버스터미널(가상정류소)", 42061), ("한라병원", 41863), ("제주대학교", 39294)])
    check("Q14 ID 기준", (q14.summary["byStationId"][0]["stationName"], clean(q14.summary["byStationId"][0]["value"])), ("제주시청(광양방면)", 32430))
    check("Q14 일수", (q14.summary["daysUsed"], len(q14.summary["excludedDays"])), (26, 5))
    check("Q14 기준을 한계로 알림", ("26일 기준" in q14.limits[0], "제주시청(광양방면) 32,430명" in q14.limits[1]), (True, True))

    q15 = analyze_weather_effect(date="2026-10-24")
    check("Q15 말할 수 없음", (q15.status, q15.summary["dateCheck"]["isPartialBusDay"], q15.summary["availablePeriod"].split("~")[1]), ("not_comparable", True, "2025-10-19"))
    check("Q15 장기 값", rows_of(q15)[2]["changePct"], -10.8)

    q16 = compare_periods("bus_station", "2025-10", "2025-10", "2026-10", "2026-10")
    check("Q16 비교 불가", (q16.status, "정의" in q16.message), ("not_comparable", True))

    check("유동인구는 2022-07을 넘지 않음", compare_periods("floating_population", "2019", "2019", "2024", "2024").status, "not_comparable")
    check("유동인구 추세도 같음", get_trend("floating_population", start="2021", end="2023").status, "not_comparable")
    check("모호한 관광지 되묻기", resolve_spot("폭포").status, "needs_clarification")
    check("없는 관광지", resolve_spot("없는관광지").status, "not_found")
    check("잘못된 입력은 예외가 아니라 결과", execute_tool("get_trend", {"metric": "없는지표"}).status, "invalid_request")
    check("잘못된 기간", execute_tool("get_trend", {"metric": "arrivals", "start": "작년"}).status, "invalid_request")


def check_verify() -> None:
    result = get_trend("arrivals", start="2024", end="2024")
    check("맞는 숫자", unverified_numbers("2024년 입도객은 13,767,350명입니다.", [result]), [])
    check("줄인 숫자", unverified_numbers("약 1,377만 명입니다.", [result]), [])
    check("지어낸 숫자", unverified_numbers("입도객은 23,723,308명입니다.", [result]), ["2.37233e+07명"])
    check("계산한 비율", unverified_numbers("내국인이 86.2%입니다.", [result]), ["86.2%"])
    check("순서·날짜는 대조 안 함", unverified_numbers("2024-08부터 3곳, 5월, 2024년에", [result]), [])
    check("지어낸 기간", unverified_numbers("자료는 2010년부터 2023년까지입니다.", [result]), ["2010년"])
    check("질문에 나온 연도", unverified_numbers("2019년 자료는 없습니다.", [result], "2019년 입도객?"), [])


def check_expected_answers() -> None:
    """시험 문제의 기대 답을 그대로 말하면 숫자 대조를 통과해야 한다. 대조가 맞는 답을 막지 않는지 본다."""
    results = {
        "Q01": [find_data("관광")],
        "Q02": [find_data("카드"), describe_data("1-01")],
        "Q03": [rank_values("spot_visitors", "2024", "2024", top_n=5)],
        "Q04": [get_trend("visit_purpose", {"purpose": "레저스포츠"})],
        "Q05": [get_trend("arrivals", start="2024", end="2024")],
        "Q06": [compare_periods("spot_visitors", "2019", "2019", "2024", "2024")],
        "Q07": [compare_periods("arrivals", "2019", "2019", "2024", "2024"), compare_periods("air_passengers", "2019", "2019", "2024", "2024", {"direction": "도착"})],
        "Q08": [get_seasonality("spot_visitors", {"sigungu": "서귀포시"})],
        "Q09": [analyze_weather_effect()],
        "Q10": [resolve_spot("성산"), get_trend("spot_visitors", {"spot": "성산일출봉"}, "2025", "2025")],
        "Q11": [rank_values("visitors_city", "2025", "2025", {"visitorClass": "외지인"})],
        "Q12": [get_trend("visitors_jeju", {"visitorClass": "외지인"}, "2025", "2025")],
        "Q13": [get_trend("visitors_city", {"visitorClass": "외국인", "sigungu": "제주시"}, "2018", "2018")],
        "Q14": [rank_values("bus_station", "2026-10", "2026-10", top_n=3)],
        "Q15": [analyze_weather_effect(date="2026-10-24")],
        "Q16": [compare_periods("bus_station", "2025-10", "2025-10", "2026-10", "2026-10")],
    }
    questions = json.loads((PROJECT_ROOT / "reference" / "eval_questions.json").read_text(encoding="utf-8"))["questions"]
    for q in questions:
        check(f"{q['id']} 기대 답의 숫자", unverified_numbers(q["expectedAnswer"], results[q["id"]], q["question"]), [])


class ScriptedConversation:
    """정해진 순서로 응답을 돌려주는 가짜 LLM."""

    def __init__(self, turns: list[Turn]):
        self.turns = list(turns)
        self.sent: list = []

    def send_user(self, text: str) -> Turn:
        self.sent.append(("user", text))
        return self.turns.pop(0)

    def send_tool_results(self, outcomes) -> Turn:
        self.sent.append(("tools", outcomes))
        return self.turns.pop(0)


def _done(text: str) -> Turn:
    return Turn(text, [], "done", {"inputTokens": 1, "outputTokens": 1})


def _tool(tool_name: str, **arguments) -> Turn:
    return Turn("", [ToolCall(f"call_{tool_name}", tool_name, arguments)], "tool_use", {"inputTokens": 1, "outputTokens": 1})


def check_loop() -> None:
    llm = ScriptedConversation([_tool("get_trend", metric="arrivals", start="2024", end="2024"), _done("13,767,350명입니다.")])
    answer = Agent(llm).ask("2024년 입도객?")
    check("루프: 정상", (answer.status, answer.unverified, [c["name"] for c in answer.tool_calls]), ("ok", [], ["get_trend"]))
    check("루프: 결과를 돌려보냄", json.loads(llm.sent[1][1][0].content)["rows"][0]["value"], 13767350)
    check("루프: 사용량 합산", answer.usage["inputTokens"], 2)

    llm = ScriptedConversation([_tool("get_trend", metric="arrivals", start="2024", end="2024"), _done("23,723,308명입니다."), _done("13,767,350명입니다.")])
    answer = Agent(llm).ask("2024년 입도객?")
    check("루프: 숫자가 틀리면 다시 쓰게 함", (answer.text, answer.unverified, llm.sent[2][0]), ("13,767,350명입니다.", [], "user"))

    llm = ScriptedConversation([_tool("get_trend", metric="arrivals", start="2024", end="2024"), _done("99,999명입니다."), _done("여전히 99,999명입니다.")])
    answer = Agent(llm).ask("입도객?")
    check("루프: 고쳐지지 않으면 표시함", answer.unverified, ["99999명"])

    llm = ScriptedConversation([_done("2026년 자료는 아직 없습니다."), _tool("get_trend", metric="arrivals", start="2024", end="2024"), _done("13,767,350명입니다.")])
    answer = Agent(llm).ask("2026년 입도객?")
    check("루프: 도구 없이 답하면 되돌림", (answer.text, [c["name"] for c in answer.tool_calls]), ("13,767,350명입니다.", ["get_trend"]))

    llm = ScriptedConversation([_done("안녕하세요."), _done("안녕하세요.")])
    check("루프: 인사는 두 번째에 그대로 받음", Agent(llm).ask("안녕").text, "안녕하세요.")

    llm = ScriptedConversation([_tool("get_trend", metric="없는지표"), _done("지표를 찾지 못했습니다.")])
    Agent(llm).ask("?")
    check("루프: 잘못된 입력은 오류로 돌려보냄", llm.sent[1][1][0].is_error, True)

    llm = ScriptedConversation([_tool("resolve_spot", name="폭포") for _ in range(9)])
    check("루프: 단계 상한", Agent(llm, max_steps=3).ask("?").status, "step_limit")
    check("루프: 거절", Agent(ScriptedConversation([Turn("", [], "refused")])).ask("?").status, "refused")
    check("루프: 잘림", Agent(ScriptedConversation([Turn("끊", [_tool("find_data").tool_calls[0]], "truncated")])).ask("?").status, "truncated")

    class Failing:
        def send_user(self, text):
            raise LLMError("인증 실패")

    check("루프: 호출 실패", Agent(Failing()).ask("?").status, "llm_error")

    agent = Agent(ScriptedConversation([_tool("resolve_spot", name="폭포"), _done("어느 폭포인가요?"), _tool("get_trend", metric="spot_visitors", filters={"spot": "천지연폭포"}, start="2024", end="2024"), _done("890,241명입니다.")]))
    agent.ask("폭포 방문객?")
    check("루프: 되묻기 뒤 이어서 답함", agent.ask("천지연").unverified, [])


class _FakeClaude(BaseHTTPRequestHandler):
    """실제 API와 같은 형식으로 답하는 가짜 서버. 보낸 요청을 기록한다."""

    requests: list[dict] = []
    responses: list[tuple[int, dict]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "beta": self.headers.get("anthropic-beta"), "body": body})
        status, payload = type(self).responses.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _message(content: list[dict], stop_reason: str) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3},
    }


def check_claude_request() -> None:
    """실제 호출은 아니지만 SDK가 만드는 요청 모양과 응답 해석, 오류 처리를 본다."""
    server = HTTPServer(("127.0.0.1", 0), _FakeClaude)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
    try:
        _FakeClaude.responses = [
            (200, _message([{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "toolu_1", "name": "get_trend", "input": {"metric": "arrivals", "start": "2024", "end": "2024"}}], "tool_use")),
            (200, _message([{"type": "text", "text": "13,767,350명입니다."}], "end_turn")),
        ]
        answer = Agent(ClaudeConversation(SYSTEM_PROMPT, TOOL_DEFINITIONS, client=client)).ask("2024년 입도객?")
        check("Claude: 답변", (answer.status, answer.text, answer.usage["cacheReadTokens"]), ("ok", "13,767,350명입니다.", 14))
        first, second = _FakeClaude.requests[0], _FakeClaude.requests[1]
        check("Claude: 모델과 대체 설정", (first["body"]["model"], first["body"]["fallbacks"], first["beta"]), ("claude-opus-5-5", "default", "server-side-fallback-2026-07-01"))
        check("Claude: 캐시와 노력 수준", (first["body"]["cache_control"], first["body"]["output_config"]), ({"type": "ephemeral"}, {"effort": "medium"}))
        check("Claude: 도구 수", len(first["body"]["tools"]), 9)
        check("Claude: 생각 블록을 그대로 돌려보냄", second["body"]["messages"][1]["content"][0]["signature"], "sig")
        check("Claude: 도구 결과", second["body"]["messages"][2]["content"][0]["tool_use_id"], "toolu_1")

        _FakeClaude.responses = [(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid"}})]
        failed = Agent(ClaudeConversation(SYSTEM_PROMPT, TOOL_DEFINITIONS, client=client)).ask("?")
        check("Claude: 인증 오류를 쉬운 말로", (failed.status, "ANTHROPIC_API_KEY" in failed.text), ("llm_error", True))
    finally:
        server.shutdown()


def _chat(message: dict, finish_reason: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish_reason}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def check_openai_request() -> None:
    """Gemini, Ollama 가 쓰는 OpenAI 호환 호출의 요청 모양과 재시도, 오류 처리를 본다."""
    server = HTTPServer(("127.0.0.1", 0), _FakeClaude)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"

    def conversation():
        return OpenAICompatibleConversation(SYSTEM_PROMPT, TOOL_DEFINITIONS, url, "test-model", "test-key", "시험", retry_wait=0)

    try:
        call = {"id": "c1", "type": "function", "function": {"name": "get_trend", "arguments": json.dumps({"metric": "arrivals", "start": "2024", "end": "2024"})}, "extra_content": {"google": {"thought_signature": "sig"}}}
        _FakeClaude.requests = []
        _FakeClaude.responses = [
            (429, {"error": "busy"}),
            (200, _chat({"content": None, "tool_calls": [call]}, "stop")),
            (200, _chat({"content": "13,767,350명입니다."}, "stop")),
        ]
        answer = Agent(conversation()).ask("2024년 입도객?")
        check("호환: 답변", (answer.status, answer.text, answer.usage["inputTokens"]), ("ok", "13,767,350명입니다.", 20))
        check("호환: 429 뒤 다시 보냄", len(_FakeClaude.requests), 3)
        last = _FakeClaude.requests[-1]["body"]
        check("호환: 도구 정의 변환", last["tools"][0]["function"]["name"], "find_data")
        check("호환: 모델이 준 메시지를 그대로 돌려보냄", last["messages"][2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"], "sig")
        check("호환: 도구 결과", (last["messages"][3]["role"], last["messages"][3]["tool_call_id"]), ("tool", "c1"))

        _FakeClaude.responses = [(200, _chat({"tool_calls": [{"id": "c2", "type": "function", "function": {"name": "get_trend", "arguments": "{깨짐"}}]}, "tool_calls")), (200, _chat({"content": "다시 물어봐 주세요."}, "stop"))]
        answer = Agent(conversation()).ask("?")
        check("호환: 깨진 인자는 입력 오류로 돌려보냄", answer.tool_calls[0]["status"], "invalid_request")

        _FakeClaude.responses = [(401, {"error": "bad key"})]
        failed = Agent(conversation()).ask("?")
        check("호환: 인증 오류", (failed.status, "키" in failed.text), ("llm_error", True))
        _FakeClaude.responses = [(429, {"error": "quota"})] * 4
        check("호환: 한도 초과", "한도" in Agent(conversation()).ask("?").text, True)
        _FakeClaude.requests = []
        _FakeClaude.responses = [(429, {"error": "requests per day limit exceeded"})]
        check("호환: 하루 한도는 기다리지 않음", ("한도" in Agent(conversation()).ask("?").text, len(_FakeClaude.requests)), (True, 1))
        _FakeClaude.responses = [(200, _chat({"content": "끊"}, "length"))]
        check("호환: 잘림", Agent(conversation()).ask("?").status, "truncated")
    finally:
        server.shutdown()


def check_display() -> None:
    check("화면: 물결표와 달러 표시 유지", safe_markdown("2019~2024, $5"), r"2019\~2024, \$5")
    check("화면: 줄바꿈 유지", safe_markdown("답\n**근거**"), "답  \n**근거**")

    samples = [
        get_trend("visit_purpose", {"purpose": "레저"}),
        get_trend("arrivals", start="2024", end="2024", grain="month"),
        get_trend("spot_visitors", {"spot": "성산일출봉"}, start="2023", end="2024", group_by="visitorType"),
        rank_values("spot_visitors", "2024", "2024", top_n=5),
        rank_values("bus_station", "2026-10", "2026-10", top_n=5),
        compare_periods("spot_visitors", "2019", "2019", "2024", "2024"),
        get_seasonality("spot_visitors", {"sigungu": "서귀포시"}),
        analyze_weather_effect(),
        get_day_pattern("visitors_jeju", {"visitorClass": "외지인"}),
    ]
    for result in samples:
        figure = build_figure(result)
        check(f"화면: 차트 생성 {result.chart['title']}", figure is not None, True)
        unknown = [c for c in rows_frame(result.rows).columns if c.isascii()]
        check(f"화면: 한글 열 이름 {result.chart['title']}", unknown, [])
        csv = csv_bytes(rows_frame(result.rows))
        check(f"화면: CSV 는 BOM 으로 시작 {result.chart['title']}", csv[:3], b"\xef\xbb\xbf")

    check("화면: 쉼표는 값에만", count_columns(rows_frame(samples[3].rows)), ["값"])
    ranked = build_figure(samples[3])
    check("화면: 순위는 눕히고 1위가 맨 위", (ranked.data[0].orientation, ranked.layout.yaxis.autorange, ranked.data[0].y[0]), ("h", "reversed", "한라산국립공원"))
    check("화면: 가장 큰 값만 적음", [t for t in ranked.data[0].text if t], ["928,409명"])
    trend = build_figure(samples[0])
    check("화면: 일부 달만 있는 해는 흐리게", (trend.data[0].marker.opacity[0], trend.data[0].marker.opacity[1]), (DIMMED_OPACITY, 1))
    check("화면: 흐린 막대 설명", "일부 달" in chart_note(samples[0]), True)
    check("화면: 계절성 x축은 월", build_figure(samples[6]).data[0].x[0], "1월")
    check("화면: 요일은 세로 막대이고 축 단위는 짧게", (build_figure(samples[8]).data[0].orientation, build_figure(samples[8]).layout.yaxis.ticksuffix), ("v", "명"))
    check("화면: 계열은 범례로 구분", (len(build_figure(samples[2]).data), build_figure(samples[2]).layout.showlegend), (2, True))
    check("화면: 값이 하나면 차트 없음", build_figure(rank_values("spot_visitors", "2024", "2024", top_n=1)), None)
    check("화면: 차트 없는 결과", build_figure(find_data("버스")), None)

    # 근거 표시는 도구 결과를 그대로 옮긴다. 지표 ID 를 사용자에게 보이지 않는다
    labels = dict(evidence_lines(samples[3]))
    check("화면: 근거", (labels["지표"], labels["기간"], labels["범위"]), ("관광지 입장객", "2024-01~2024-12", "내국인+외국인 합산"))

    answer = AgentAnswer("", tool_calls=[{"name": "resolve_spot", "arguments": {"name": "폭포"}, "status": "needs_clarification"}], results=[resolve_spot("폭포")])
    check("화면: 되묻기 후보", clarification_choices(answer), ["정방폭포", "천제연폭포", "천지연폭포"])
    answer = AgentAnswer("", tool_calls=[{"name": "get_trend", "arguments": {}, "status": "ok"}] * 2 + [{"name": "get_trend", "arguments": {"metric": "없음"}, "status": "invalid_request"}], results=[samples[0], samples[0], get_trend("없음")])
    check("화면: 다시 부른 결과와 입력 오류는 뺌", len(visible_results(answer)), 1)

    hub_only = ToolResult("ok", display_only={"hubMatches": [{"title": "자료", "category": "분야", "owner": "기관", "periodText": "기간", "updatedDate": "날짜", "viewCount": 1}]})
    check("화면: 허브 표는 정해진 열만", list(hub_frame(hub_only).columns), ["자료 이름", "분야", "제공 기관", "제공 기간", "갱신일"])
    check("화면: 허브 항목은 LLM에 안 감", "자료 이름" in hub_only.to_llm_json() or "hubMatches" in hub_only.to_llm_json(), False)


def _app_test(turns: list[Turn]):
    """가짜 LLM으로 실제 화면 코드를 돌린다."""
    from streamlit.testing.v1 import AppTest

    agent_module.make_conversation = lambda system, tools: ScriptedConversation(turns)
    return AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=60).run()


def _kinds(app, kind: str) -> int:
    return len(app.get(kind))


def check_app() -> None:
    original = agent_module.make_conversation
    try:
        app = _app_test([_tool("rank_values", metric="spot_visitors", start="2024", end="2024", top_n=5), _done("한라산국립공원이 928,409명으로 1위입니다. 2024-01~2024-12 자료 기준입니다.")])
        check("앱: 첫 화면 오류 없음", len(app.exception), 0)
        check("앱: 코드의 값이 화면에 찍히지 않음", [m.value for m in app.markdown if "None" in m.value], [])
        check("앱: 첫 화면 예시 버튼", sum(b.key.startswith("main_") for b in app.button), 6)

        app.chat_input[0].set_value("2024년에 사람이 가장 많이 간 관광지 5곳").run()
        check("앱: 답변 뒤 오류 없음", [e.value for e in app.exception], [])
        check("앱: 차트 표시", _kinds(app, "plotly_chart"), 1)
        check("앱: CSV 내려받기", _kinds(app, "download_button"), 1)
        check("앱: 물결표가 취소선이 되지 않음", any(r"2024-01\~2024-12" in m.value for m in app.markdown), True)
        check("앱: 입력창 다시 켜짐", app.chat_input[0].disabled, False)
        check("앱: 근거 표시", any("계산 방법" in m.value for m in app.markdown), True)

        app = _app_test([_tool("resolve_spot", name="폭포"), _done("어느 폭포인가요?"), _tool("get_trend", metric="spot_visitors", filters={"spot": "천지연폭포"}, start="2024", end="2024"), _done("890,241명입니다.")])
        app.button(key="main_0").click().run()
        check("앱: 예시 버튼으로 질문", [e.value for e in app.exception], [])
        choice = [b for b in app.button if b.key.startswith("choice_")]
        check("앱: 되묻기 후보 버튼", [b.label for b in choice], ["정방폭포", "천제연폭포", "천지연폭포"])
        next(b for b in choice if b.label == "천지연폭포").click().run()
        check("앱: 후보를 고르면 이어서 답함", any("890,241명입니다." in m.value for m in app.markdown), True)
        check("앱: 고른 뒤 후보 버튼 사라짐", [b.key for b in app.button if b.key.startswith("choice_")], [])

        app = _app_test([_tool("get_trend", metric="arrivals", start="2024", end="2024"), _done("99,999명입니다."), _done("여전히 99,999명입니다.")])
        app.chat_input[0].set_value("입도객?").run()
        check("앱: 확인되지 않은 숫자 경고", any("99999명" in w.value for w in app.warning), True)

        def failing(system, tools):
            raise LLMError(".env 에 GEMINI_API_KEY 가 없습니다.")

        app = _app_test([])
        agent_module.make_conversation = failing
        app.chat_input[0].set_value("입도객?").run()
        check("앱: 키가 없으면 오류를 안내", ([e.value for e in app.exception], [e.value for e in app.error]), ([], [".env 에 GEMINI_API_KEY 가 없습니다."]))

        # 정해진 응답이 없어 가짜 LLM이 예외를 낸다
        app = _app_test([])
        app.chat_input[0].set_value("입도객?").run()
        check("앱: 예상 못한 오류 뒤 입력창 복구", ([e.value for e in app.exception], len(app.error), app.chat_input[0].disabled), ([], 1, False))

        app = _app_test([])
        app.sidebar.radio[0].set_value("데이터 목록").run()
        check("앱: 데이터 목록", ([e.value for e in app.exception], any("분석할 수 있는 자료" in s.value for s in app.subheader)), ([], True))
        app.text_input[0].set_value("카드").run()
        check("앱: 데이터 검색의 센터 전용 명세", ([e.value for e in app.exception], any("센터 전용" in s.value for s in app.subheader)), ([], True))
        if find_data("버스").display_only.get("hubMatches"):
            app.text_input[0].set_value("버스").run()
            check("앱: 허브 목록은 표만 있고 내려받기 없음", (any("허브 목록" in s.value for s in app.subheader), _kinds(app, "download_button")), (True, 0))
    finally:
        agent_module.make_conversation = original


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    check_tools()
    check_verify()
    check_expected_answers()
    check_loop()
    check_claude_request()
    check_openai_request()
    check_display()
    check_app()
    print(f"확인 {_passed + _failed}건, 다름 {_failed}건")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
