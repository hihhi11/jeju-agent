"""LLM에게 보여 줄 도구 정의와 실행. 입력은 스키마로 확인한 뒤 함수에 넘긴다."""

from __future__ import annotations

import traceback

from jsonschema import Draft202012Validator

from agent import tools
from agent.center import SPECS as CENTER_SPECS, CenterStore, analyze_center_csv
from agent.common import ToolResult
from agent.metrics import METRICS

_FILTERS = {
    "type": "object",
    "description": "지표별 필터. 쓸 수 있는 키는 지표 설명에 있다.",
    "properties": {
        "spot": {"type": "string", "description": "관광지 이름"},
        "sigungu": {"type": "string", "enum": ["제주시", "서귀포시"]},
        "category": {"type": "string", "description": "관광지 분류 또는 음식점 원본 소분류"},
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
        "industry": {"type": "string", "description": "카드 이용 업종 (예: 한식 음식점업, 호텔업, 면세점)"},
        "spenderGroup": {"type": "string", "enum": ["관광객(추정)", "제주도민", "법인"]},
        "userType": {"type": "string", "enum": ["내국인", "제주도민", "법인", "중국", "일본", "동남아", "기타외국"]},
        "timeSlot": {"type": "string", "enum": ["오전", "점심", "오후", "저녁", "밤"]},
        "menu": {"type": "string", "enum": ["밥류", "면류", "죽류", "국/탕/찌개/전골류", "구이류", "볶음류", "튀김류", "찜류", "조림류", "데침/삶음류", "적/꼬치류", "전/부침류", "만두류", "회류", "장/장아찌류", "냉채/무침류", "김치류", "묵류", "떡류", "빵/과자류", "과일/샐러드류", "주류", "음료류", "빙과류", "추가/사리", "기타"], "description": "외식 메뉴 대분류. 회는 회류, 고기는 구이류, 술은 주류, 커피는 음료류"},
        "region": {"type": "string", "description": "경기지수 지역 (전국, 제주, 서울 등 시도)"},
        "outlook": {"type": "string", "enum": ["체감", "전망"]},
        "area": {"type": "string", "description": "읍면동 (예: 애월읍, 연동). 음식점은 법정동(일도일동), 방문객·차량 도착은 행정동(일도1동)"},
        "foodGroup": {"type": "string", "enum": ["카페", "한식", "주점", "치킨", "고기구이", "횟집", "일식", "중식", "양식", "외국음식", "분식", "패스트푸드", "제과·아이스크림", "편의점", "기타"], "description": "음식점 업종 묶음. 카페는 커피숍·다방 등을 묶은 값"},
        "businessType": {"type": "string", "enum": ["일반음식점", "휴게음식점"]},
        "passengerType": {"type": "string", "enum": ["한국인", "외국인"], "description": "크루즈 승객 구분"},
        "ship": {"type": "string", "description": "크루즈 선박명"},
        "carType": {"type": "string", "enum": ["승용차", "승합차"]},
        "item": {"type": "string", "enum": ["숙박비", "쇼핑비", "식음료비", "대중교통비", "차량임대비", "관광 및 문화", "오락 및 운동", "기타비", "항공 및 선박료"], "description": "관광객 1인당 지출 항목. 렌터카 비용은 차량임대비"},
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
        "get_day_pattern",
        "요일별 하루 평균과 평일·주말 차이. 일별 자료인 visitors_jeju, visitors_city, bus_station 만 된다. 기간을 안 주면 가장 최근의 온전한 12개월을 쓴다.",
        {
            "metric": _METRIC,
            "filters": _FILTERS,
            "start": _MONTH,
            "end": _MONTH,
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
    _tool(
        "list_events",
        "제주 주요 행사(축제, 공연, 전시)를 기간이나 이름으로 찾는다. 그 기간에 무슨 행사가 있었는지 물을 때 쓴다. 행사 수가 늘었는지 비교하는 데는 쓰지 않는다.",
        {
            "start": _MONTH,
            "end": _MONTH,
            "keyword": {"type": "string", "description": "행사 이름에 들어갈 낱말 (예: 들불, 수국)"},
        },
    ),
    _tool(
        "analyze_center_csv",
        "사용자가 제주빅데이터센터에서 내려받아 넣은 CSV를 집계한다. 1-08 카드 이용 현황(읍면동)_v02: 법정동·권역·업종·이용자·연령·성별별 이용금액(use_amt)·이용건수(use_cnt)·이용자수(user_cnt). "
        "4-15 단기체류 외국인 유동인구(읍면동)_v02: 행정동·권역·국적·시간별 시간당 평균 방문인구(visit_pop). "
        "CSV를 넣지 않았으면 center_only 와 내려받는 방법(summary.download)을 돌려준다. 10 미만 칸은 도구가 상위 단위로 묶거나 가리므로 묶음이 바뀔 수 있다(summary.groupBy).",
        {
            "dataset": {"type": "string", "enum": list(CENTER_SPECS)},
            "measure": {"type": "string", "enum": sorted({m for s in CENTER_SPECS.values() for m in s.measures})},
            "group_by": {"type": "array", "items": {"type": "string", "enum": sorted({d for s in CENTER_SPECS.values() for d in s.dims})}, "maxItems": 2, "description": "묶을 기준 2개까지. 순위나 비교 대상을 첫째로"},
            "filters": {
                "type": "object",
                "description": "조건. 키는 group_by 와 같은 이름(month 는 YYYY-MM, dong 은 '애월' 처럼 일부만 써도 된다). userType 에 '외국인' 을 주면 외국 유형 4개를 합친다",
                "additionalProperties": {"type": "string"},
            },
            "start": _MONTH,
            "end": _MONTH,
        },
        ["dataset"],
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
    "get_day_pattern": tools.get_day_pattern,
    "analyze_weather_effect": tools.analyze_weather_effect,
    "list_events": tools.list_events,
}
_VALIDATORS = {d["name"]: Draft202012Validator(d["input_schema"]) for d in TOOL_DEFINITIONS}


def execute_tool(name: str, arguments: dict, center_store: CenterStore | None = None) -> ToolResult:
    """도구를 실행한다. 잘못된 입력과 예외는 LLM이 고칠 수 있게 결과로 돌려준다.

    center_store 는 화면 세션에 넣은 센터 CSV다. 센터 도구에만 넘긴다.
    """
    if name == "analyze_center_csv":
        function = lambda **kwargs: analyze_center_csv(center_store, **kwargs)  # noqa: E731
    elif name in _FUNCTIONS:
        function = _FUNCTIONS[name]
    else:
        return ToolResult("invalid_request", f"없는 도구입니다: {name}")
    errors = sorted(_VALIDATORS[name].iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if errors:
        return ToolResult("invalid_request", "입력이 올바르지 않습니다: " + "; ".join(e.message for e in errors[:3]))
    try:
        return function(**arguments)
    except ValueError as exc:
        return ToolResult("invalid_request", str(exc))
    except Exception:
        traceback.print_exc()
        return ToolResult("error", "도구 실행 중 오류가 났습니다. 이 결과로 수치를 말하지 마세요.")
