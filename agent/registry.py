"""LLM에게 보여 줄 도구 정의와 실행. 입력은 스키마로 확인한 뒤 함수에 넘긴다."""

from __future__ import annotations

import traceback

from jsonschema import Draft202012Validator

from agent import tools
from agent.common import ToolResult
from agent.metrics import METRICS

_FILTERS = {
    "type": "object",
    "description": "지표별 필터. 쓸 수 있는 키는 지표 설명에 있다.",
    "properties": {
        "spot": {"type": "string", "description": "관광지 이름"},
        "sigungu": {"type": "string", "enum": ["제주시", "서귀포시"]},
        "category": {"type": "string"},
        "visitorType": {"type": "string", "enum": ["내국인", "외국인"]},
        "purpose": {"type": "string", "description": "방문목적 또는 여행형태 항목 (예: 레저스포츠, 개별여행)"},
        "classType": {"type": "string", "enum": ["방문목적", "여행형태"]},
        "nationality": {"type": "string"},
        "direction": {"type": "string", "enum": ["도착", "출발", "계"]},
        "airport": {"type": "string"},
        "sex": {"type": "string"},
        "ageRange": {"type": "string"},
        "visitorClass": {"type": "string", "enum": ["현지인", "외지인", "외국인"]},
        "name": {"type": "string", "description": "정류소 이름"},
        "stationId": {"type": "string"},
        "moveType": {"type": "string", "enum": ["승차", "환승"]},
        "priceType": {"type": "string"},
    },
    "additionalProperties": False,
}
_METRIC = {"type": "string", "enum": list(METRICS)}
_MONTH = {"type": "string", "description": "YYYY 또는 YYYY-MM"}
_GROUP = {"type": "string", "description": "묶을 기준. 필터 키 중 하나"}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
    }


TOOL_DEFINITIONS = [
    _tool(
        "find_data",
        "질문에 맞는 데이터를 찾는다. 어떤 데이터가 있는지 묻거나 분석 가능한 자료인지 모를 때 쓴다. 센터 전용 명세와 허브 목록 검색 결과도 함께 돌려준다.",
        {"query_text": {"type": "string", "description": "질문의 핵심 키워드. 전체 목록을 보려면 빈 문자열"}},
    ),
    _tool(
        "describe_data",
        "데이터 하나의 항목, 기간, 한계를 DB에서 읽는다. find_data 가 돌려준 id 나 센터 번호(예: 1-01)를 넣는다.",
        {"data_id": {"type": "string"}},
        ["data_id"],
    ),
    _tool(
        "resolve_spot",
        "사용자가 말한 관광지 이름을 자료의 이름으로 확인한다. 후보가 여럿이면 needs_clarification 을 돌려준다.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "get_trend",
        "기간별 추세와 전체 합계. 연·월 단위 값, 전년 대비, 최근 12개월과 그 전 12개월 비교(summary.recent12Months)를 돌려준다. 값 하나를 물을 때도 이 도구를 쓴다.",
        {
            "metric": _METRIC,
            "filters": _FILTERS,
            "start": _MONTH,
            "end": _MONTH,
            "grain": {"type": "string", "enum": ["year", "month"]},
            "group_by": _GROUP,
        },
        ["metric"],
    ),
    _tool(
        "rank_values",
        "대상별 순위. 기간을 안 주면 가장 최근의 온전한 해를 쓴다. 달이 다 차지 않은 대상은 순위에서 빼고 summary.excluded 에 알린다.",
        {
            "metric": _METRIC,
            "start": _MONTH,
            "end": _MONTH,
            "filters": _FILTERS,
            "group_by": _GROUP,
            "top_n": {"type": "integer", "minimum": 1, "maximum": 50},
            "ascending": {"type": "boolean"},
        },
        ["metric"],
    ),
    _tool(
        "compare_periods",
        "두 기간 비교와 코로나 전 대비 회복률. A가 기준, B가 비교 대상이다. 두 기간의 달이 모두 온전한 대상만 계산한다. 비교할 수 없으면 not_comparable 과 alternatives 를 돌려준다.",
        {
            "metric": _METRIC,
            "period_a_start": _MONTH,
            "period_a_end": _MONTH,
            "period_b_start": _MONTH,
            "period_b_end": _MONTH,
            "filters": _FILTERS,
            "group_by": _GROUP,
        },
        ["metric", "period_a_start", "period_a_end", "period_b_start", "period_b_end"],
    ),
    _tool(
        "get_seasonality",
        "월별 성수기·비수기 패턴. 코로나 기간은 기본으로 뺀다. 해마다 1위 달과, 같은 대상만의 평균을 돌려준다.",
        {
            "metric": _METRIC,
            "filters": _FILTERS,
            "start_year": {"type": "integer"},
            "end_year": {"type": "integer"},
            "include_covid": {"type": "boolean"},
        },
        ["metric"],
    ),
    _tool(
        "analyze_weather_effect",
        "강수량이나 기온에 따른 버스 승차 변화를 평소와 비교한다. 특정 날짜를 물으면 date 를 넣는다. 버스와 날씨의 관계는 이 도구로만 답한다.",
        {
            "weather": {"type": "string", "enum": ["rain", "temperature"]},
            "bins": {"type": "array", "items": {"type": "number"}, "description": "구간 경계. 안 주면 강수 1, 10, 30mm"},
            "start": _MONTH,
            "end": _MONTH,
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
    ),
]

_FUNCTIONS = {
    "find_data": tools.find_data,
    "describe_data": tools.describe_data,
    "resolve_spot": tools.resolve_spot,
    "get_trend": tools.get_trend,
    "rank_values": tools.rank_values,
    "compare_periods": tools.compare_periods,
    "get_seasonality": tools.get_seasonality,
    "analyze_weather_effect": tools.analyze_weather_effect,
}
_VALIDATORS = {d["name"]: Draft202012Validator(d["input_schema"]) for d in TOOL_DEFINITIONS}


def execute_tool(name: str, arguments: dict) -> ToolResult:
    """도구를 실행한다. 잘못된 입력과 예외는 LLM이 고칠 수 있게 결과로 돌려준다."""
    if name not in _FUNCTIONS:
        return ToolResult("invalid_request", f"없는 도구입니다: {name}")
    errors = sorted(_VALIDATORS[name].iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if errors:
        return ToolResult("invalid_request", "입력이 올바르지 않습니다: " + "; ".join(e.message for e in errors[:3]))
    try:
        return _FUNCTIONS[name](**arguments)
    except ValueError as exc:
        return ToolResult("invalid_request", str(exc))
    except Exception:
        traceback.print_exc()
        return ToolResult("error", "도구 실행 중 오류가 났습니다. 이 결과로 수치를 말하지 마세요.")
