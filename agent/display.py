"""도구 결과를 화면용 표와 차트로 바꾼다. Streamlit 을 쓰지 않아 화면 없이 시험할 수 있다."""

from __future__ import annotations

import re

import pandas as pd
import plotly.graph_objects as go

from agent.agent import AgentAnswer
from agent.common import ToolResult, clean

# 차트 색. 범주 순서는 색약 대조를 통과한 순서라 바꾸지 않는다
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
TEXT_COLOR = "#52514e"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
SURFACE_COLOR = "#fcfcfb"
FONT_FAMILY = '"Malgun Gothic", system-ui, "Segoe UI", sans-serif'
# 이보다 많으면 색으로 구분할 수 없어 표로만 보여 준다
MAX_SERIES = len(SERIES_COLORS)
# 이 길이부터 이름이 긴 막대는 가로로 눕히면 읽기 쉽다
TIME_AXES = {"period", "month", "range", "dayOfWeek", "quarter", "year", "hour"}
DIMMED_OPACITY = 0.4

COLUMN_LABELS = {
    "period": "기간",
    "value": "값",
    "months": "달 수",
    "isComplete": "달이 모두 있음",
    "yoyPct": "전년 대비(%)",
    "spotCount": "관광지 수",
    "visitorType": "내·외국인",
    "rank": "순위",
    "spot": "관광지",
    "spotName": "관광지",
    "name": "정류소",
    "stationId": "정류소 ID",
    "stationName": "정류소",
    "valueA": "기준 기간 값",
    "valueB": "비교 기간 값",
    "ratioPct": "기준 대비(%)",
    "changePct": "변화율(%)",
    "reachedBase": "기준 이상",
    "isSmallBase": "기준 값이 작음",
    "month": "월",
    "dayOfWeek": "요일",
    "industry": "업종",
    "spenderGroup": "이용자 구분",
    "userType": "이용자",
    "timeSlot": "시간대",
    "sex": "성별",
    "ageRange": "연령대",
    "nationality": "국가",
    "purpose": "목적",
    "classType": "구분",
    "direction": "방향",
    "airport": "공항",
    "visitorClass": "방문자 구분",
    "moveType": "승차·환승",
    "priceType": "요금 유형",
    "latestYearValue": "최근 해 값",
    "averageValue": "평균",
    "latestYearSpotCount": "최근 해 관광지 수",
    "range": "구간",
    "days": "일수",
    "isSufficient": "표본 충분",
    "id": "데이터 ID",
    "title": "자료 이름",
    "topic": "기능",
    "grain": "단위",
    "startDate": "시작",
    "endDate": "끝",
    "description": "설명",
    "limits": "주의사항",
    "columnName": "항목",
    "columnNameKo": "항목 이름",
    "columnType": "형식",
    "dataType": "형식",
    "sigungu": "시군",
    "category": "분류",
    "firstMonth": "첫 달",
    "lastMonth": "마지막 달",
    "centerNo": "센터 번호",
    "exportable": "반출 가능",
    "provider": "제공처",
    "region": "지역",
    "outlook": "체감·전망",
    "area": "읍면동",
    "foodGroup": "업종 묶음",
    "businessType": "음식점 구분",
    "yoyPoints": "전년 대비(포인트)",
    "changePoints": "변화(포인트)",
    "eventName": "행사",
    "durationDays": "일수",
    "isLongRunning": "90일 넘게 열림",
    "item": "지출 항목",
    "passengerType": "승객 구분",
    "ship": "선박",
    "carType": "차종",
    "dong": "읍면동",
    "hour": "시간",
    "quarter": "분기",
    "year": "연",
    "userGroup": "이용자 구분",
    "tourType": "관광구분",
    "age": "연령대",
    "areaType": "도심·읍면",
    "suppressed": "값 가림(10명 미만)",
    "menu": "메뉴",
}
EVIDENCE_LABELS = {
    "label": "지표",
    "period": "기간",
    "method": "계산 방법",
    "visitorScope": "범위",
    "filters": "조건",
    "rowCount": "행 수",
    "catalogId": "데이터 ID",
    "table": "사용한 표",
}
# 시험 전 값이나 입력 오류는 근거로 보여 주지 않는다
HIDDEN_STATUSES = {"invalid_request", "error"}


def safe_markdown(text: str) -> str:
    """Streamlit 마크다운은 ~ 를 취소선, $ 를 수식으로 읽어 "2019~2024" 가 깨지고, 줄바꿈 하나는 무시한다."""
    return text.replace("~", r"\~").replace("$", r"\$").replace("\n", "  \n")


def visible_results(answer: AgentAnswer) -> list[tuple[str, ToolResult]]:
    """화면에 보여 줄 (도구 이름, 결과). 같은 호출을 다시 한 것과 입력 오류는 뺀다."""
    seen, shown = set(), []
    for call, result in zip(answer.tool_calls, answer.results):
        key = (call["name"], str(call["arguments"]))
        if result.status not in HIDDEN_STATUSES and key not in seen:
            seen.add(key)
            shown.append((call["name"], result))
    return shown


def result_title(result: ToolResult) -> str:
    if result.chart:
        return result.chart["title"]
    return result.evidence.get("label") or result.summary.get("title") or "찾은 자료"


def rows_frame(rows: list[dict], labels: dict | None = None) -> pd.DataFrame:
    """행을 한글 열 이름의 표로 바꾼다. 내·외국인 내역 같은 묶음 값은 열로 펼친다."""
    frame = pd.json_normalize(clean(rows))
    frame.columns = [column.removeprefix("breakdown.") for column in frame.columns]
    # 가린 칸이 하나도 없으면 '아니오'만 늘어선 열은 헷갈리기만 해서 뺀다
    if "suppressed" in frame.columns and not frame["suppressed"].any():
        frame = frame.drop(columns="suppressed")
    for column in frame.columns:
        if frame[column].map(lambda v: isinstance(v, bool)).any():
            frame[column] = frame[column].map({True: "예", False: "아니오"})
        elif frame[column].map(lambda v: isinstance(v, (list, dict))).any():
            frame[column] = frame[column].map(str)
    return frame.rename(columns=labels or COLUMN_LABELS)


def count_columns(frame: pd.DataFrame) -> list[str]:
    """화면에서 천 단위 쉼표를 붙일 열. 순서나 달을 나타내는 작은 수는 그대로 둔다."""
    return [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c]) and c not in ("순위", "월")]


def csv_bytes(frame: pd.DataFrame) -> bytes:
    """엑셀에서 한글이 깨지지 않게 BOM 을 붙인다."""
    return frame.to_csv(index=False).encode("utf-8-sig")


def evidence_lines(result: ToolResult) -> list[tuple[str, str]]:
    lines = []
    for key, label in EVIDENCE_LABELS.items():
        value = clean(result.evidence.get(key))
        if value in (None, "", {}):
            continue
        if isinstance(value, dict):
            value = ", ".join(f"{v}" for v in value.values())
        lines.append((label, f"{value:,}" if isinstance(value, int) else str(value)))
    return lines


def clarification_choices(answer: AgentAnswer) -> list[str]:
    """되물을 후보. 뒤에 다시 조회해 답을 찾았으면 고를 것이 없다."""
    last = answer.results[-1] if answer.results else None
    return list(last.summary.get("candidates", [])) if last and last.status == "needs_clarification" else []


def _unit(result: ToolResult, y: str) -> str:
    # "명(일별 합계)" 의 설명은 축에 다 쓰기에 길어 근거에서 보여 준다
    return "%" if y.endswith("Pct") else re.sub(r"\(.*\)", "", result.evidence.get("unit", ""))


def _x_text(x: str, value) -> str:
    # 계절성의 달(1~12)만 "월" 을 붙인다. 센터 자료의 "2024-07" 은 그대로 쓴다
    return f"{value}월" if x == "month" and isinstance(value, int) else str(value)


def _is_dimmed(row: dict) -> bool:
    return row.get("isComplete") is False or row.get("isSufficient") is False


def chart_note(result: ToolResult) -> str:
    """흐리게 그린 값이 무슨 뜻인지."""
    rows = clean(result.rows)
    if not result.chart or not any(_is_dimmed(r) for r in rows):
        return ""
    if any(r.get("isSufficient") is False for r in rows):
        return "흐린 막대는 표본이 부족한 구간입니다."
    return "흐린 막대는 일부 달만 있어 다른 해와 바로 비교할 수 없습니다."


def build_figure(result: ToolResult) -> go.Figure | None:
    """도구가 준 차트 설정으로 그림을 만든다. 값이 하나뿐이거나 계열이 너무 많으면 표로 충분하다."""
    spec, rows = result.chart, clean(result.rows)
    if not spec or len(rows) < 2:
        return None
    x, y, series = spec["x"], spec["y"], spec.get("series")
    groups = list(dict.fromkeys(r[series] for r in rows)) if series else [None]
    if len(groups) > MAX_SERIES:
        return None
    unit = _unit(result, y)
    # 원 단위 금액은 자릿수가 많아 축에서 읽기 어렵다. 표에는 원 단위를 그대로 둔다. 1인당 지출처럼 작은 금액은 원 그대로 쓴다
    largest = max((abs(r[y]) for r in rows if r.get(y) is not None), default=0)
    scale = 100_000_000 if unit == "원" and largest >= 100_000_000 else 1
    unit = "억 원" if scale > 1 else unit
    # 이름 순위는 눕혀서 그린다. 시간과 구간은 왼쪽에서 오른쪽으로 읽는다
    horizontal = spec["type"] == "bar" and x not in TIME_AXES
    categories = list(dict.fromkeys(_x_text(x, r[x]) for r in rows))
    scaled = lambda v: v if v is None or scale == 1 else round(v / scale, 1)  # noqa: E731
    peak = max((abs(scaled(r[y])) for r in rows if r.get(y) is not None), default=None)

    fig = go.Figure()
    for index, group in enumerate(groups):
        part = [r for r in rows if not series or r[series] == group]
        labels = [_x_text(x, r[x]) for r in part]
        values = [scaled(r.get(y)) for r in part]
        color = SERIES_COLORS[index]
        name = str(group) if series else result_title(result)
        hover = f"%{{{'y' if horizontal else 'x'}}}<br>{name}: %{{{'x' if horizontal else 'y'}:,}}{unit}<extra></extra>"
        # 값은 표와 툴팁으로 볼 수 있어 가장 큰 값 하나만 적는다
        text = [f"{v:,}{unit}" if not series and v is not None and abs(v) == peak else "" for v in values]
        if spec["type"] == "line":
            fig.add_scatter(x=labels, y=values, name=name, mode="lines+markers+text", line={"color": color, "width": 2}, marker={"color": color, "size": 8}, text=text, textposition="top center", hovertemplate=hover, cliponaxis=False)
        else:
            opacity = [DIMMED_OPACITY if _is_dimmed(r) else 1 for r in part]
            fig.add_bar(
                x=values if horizontal else labels,
                y=labels if horizontal else values,
                name=name,
                orientation="h" if horizontal else "v",
                marker={"color": color, "opacity": opacity, "cornerradius": 4},
                text=text,
                textposition="outside",
                cliponaxis=False,
                hovertemplate=hover,
            )

    value_axis = {"tickformat": ",", "ticksuffix": unit, "gridcolor": GRID_COLOR, "zerolinecolor": AXIS_COLOR, "automargin": True}
    category_axis = {"type": "category", "categoryorder": "array", "categoryarray": categories, "showgrid": False, "linecolor": AXIS_COLOR, "automargin": True}
    if horizontal:
        # 첫 행(1위)이 맨 위에 오게 뒤집는다
        category_axis["autorange"] = "reversed"
    if y == "ratioPct":
        # 100%가 기준 기간과 같은 값이다
        line = {"color": TEXT_COLOR, "width": 1}
        annotation = {"text": "기준 100%", "font": {"color": TEXT_COLOR, "size": 12}}
        if horizontal:
            fig.add_vline(x=100, line=line, annotation=annotation, annotation_position="top")
        else:
            fig.add_hline(y=100, line=line, annotation=annotation, annotation_position="top right")

    row_count = len(categories)
    fig.update_layout(
        title={"text": spec["title"], "font": {"size": 16}, "x": 0, "xanchor": "left"},
        font={"family": FONT_FAMILY, "color": TEXT_COLOR, "size": 13},
        paper_bgcolor=SURFACE_COLOR,
        plot_bgcolor=SURFACE_COLOR,
        barmode="group",
        bargap=0.35,
        # 끌어서 확대하면 비전문가가 되돌리기 어렵다
        dragmode=False,
        showlegend=bool(series),
        legend={"orientation": "h", "y": 1.02, "yanchor": "bottom", "x": 1, "xanchor": "right"},
        margin={"l": 8, "r": 56, "t": 64, "b": 8},
        height=max(340, 28 * row_count + 110) if horizontal else 380,
        xaxis=value_axis if horizontal else category_axis,
        yaxis=category_axis if horizontal else value_axis,
    )
    return fig
