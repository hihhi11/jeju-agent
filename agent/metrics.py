"""지표 목록과 조회. 표, 값 컬럼, 허용 필터, 고정 주의사항을 여기서 정해 LLM이 표를 고르지 않게 한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import pandas as pd

from agent.common import ToolResult, query

_ARRIVALS_LONG = (
    "(SELECT dtYearMonth, '내국인' AS visitorType, domesticCnt AS v FROM arrivals_monthly "
    "UNION ALL SELECT dtYearMonth, '외국인', foreignCnt FROM arrivals_monthly)"
)

SIGUNGU_NAMES = {"제주": "제주시", "서귀포": "서귀포시"}


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    unit: str
    source: str
    time_col: str
    value_col: str
    catalog_id: str
    dims: dict[str, str]
    default_group: str | None = None
    required: tuple[str, ...] = ()
    defaults: dict[str, str] = field(default_factory=dict)
    where: str = ""
    daily: bool = False
    # 합계에서 뺀 날이 있어 달이 다 차지 않아도 쓰는 지표
    partial_days_ok: bool = False
    # 이 달을 넘어 비교하지 않는다 (집계 방식 변경)
    break_month: str | None = None
    # 내·외국인을 합친 값일 수 있어 나눠 보여 주는 지표
    breakdown: str | None = None
    # 이 지표로 비교할 수 없을 때 안내할 대체 지표
    alternatives: tuple[dict, ...] = ()
    # 지수처럼 더할 수 없는 값은 여러 달을 평균한다 (mean)
    agg: str = "sum"
    # 건수라 0인 달도 자료가 있는 달이다. 행이 없는 달을 0으로 채운다
    fill_zero: bool = False
    limits: tuple[str, ...] = ()
    note: str = ""


METRICS: dict[str, Metric] = {
    m.id: m
    for m in [
        Metric(
            "spot_visitors", "관광지 입장객", "명", "spot_visitors_view", "dtYearMonth", "visitorCnt", "spot_visitors_monthly",
            {"spot": "spotName", "sigungu": "sigungu", "category": "category", "visitorType": "visitorType"},
            default_group="spot",
            breakdown="visitorType",
            limits=("입장 집계가 있는 관광지만 포함하고 무료·미집계 지점은 빠진다.", "입장객 0은 휴장 또는 미집계일 수 있어 자료가 있는 달로 세지 않는다."),
            note="관광지 50곳 월별 입장객. 시군·분류·내외국인 필터",
        ),
        Metric(
            "arrivals", "입도객", "명", _ARRIVALS_LONG, "dtYearMonth", "v", "arrivals_monthly",
            {"visitorType": "visitorType"},
            default_group="visitorType",
            breakdown="visitorType",
            alternatives=(
                {"metric": "air_passengers", "filters": {"direction": "도착"}, "label": "제주 공항 도착 이용자", "note": "입도객 수가 아니라 항공 이용자 지표다. 도민 등을 포함하고 배편을 포함하지 않는다."},
            ),
            limits=("내국인 값은 허브 자료를 대조해 내국인으로 추정한 것이다.",),
            note="제주 입도객 합계. 입도객 전체 수는 이 지표만 쓴다",
        ),
        Metric(
            "visit_purpose", "방문목적·여행형태별 입도객", "명", "visit_purpose", "dtYearMonth", "visitorCnt", "visit_purpose",
            {"purpose": "visitPurpose", "classType": "classType"},
            default_group="purpose",
            alternatives=(
                {"metric": "air_passengers", "filters": {"direction": "도착"}, "label": "제주 공항 도착 이용자", "note": "방문목적을 나눌 수 없고, 입도객 수가 아니라 항공 이용자 지표다."},
            ),
            limits=("여행형태와 방문목적은 같은 사람을 나눈 값이라 함께 더하지 않는다.",),
            note="방문목적(레저스포츠 등)·여행형태(개별여행 등)별 입도객. purpose 나 classType 필터 필수",
        ),
        Metric(
            "foreign_arrivals", "국가별 외국인 입도객", "명", "foreign_arrivals_monthly", "dtYearMonth", "jejuVisitorCnt", "foreign_arrivals_monthly",
            {"nationality": "nationality"},
            default_group="nationality",
            limits=("확정치가 없는 달은 잠정치다.",),
            note="국가별 외국인 입도객",
        ),
        Metric(
            "air_passengers", "제주 공항 이용자(항공 이용자 지표)", "명", "airport_passengers_monthly", "dtYearMonth", "passengerCnt", "airport_passengers_monthly",
            {"direction": "direction", "airport": "airport"},
            default_group="airport",
            required=("direction",),
            defaults={"airport": "제주"},
            limits=("입도객 수가 아니라 항공 이용자 지표다. 도민 등을 포함하고 배편을 포함하지 않는다.",),
            note="2019년과 비교할 수 있는 규모 지표. direction(도착/출발/계) 필수. 입도객이라고 부르지 않는다",
        ),
        Metric(
            "floating_population", "내국인 유동인구", "명(연인원)", "domestic_floating_monthly", "dtYearMonth", "visitPopulation", "domestic_floating_monthly",
            {"sex": "sex", "ageRange": "ageRange"},
            default_group="ageRange",
            break_month="2022-07",
            limits=("2022-07에 집계 방식이 달라진 것으로 보여 이 시점을 넘어 비교하지 않는다.", "같은 조건에 값이 2개인 행이 있고 어느 값이 맞는지 알 수 없어 그대로 더했다."),
            note="월·성별·연령별 내국인 유동인구. 2022-07을 넘는 비교 불가",
        ),
        Metric(
            "visitors_jeju", "제주 전체 방문자 추정치", "명(일별 합계)", "tour_metro_daily", "baseDate", "visitors", "tour_metro_daily",
            {"visitorClass": "visitorClass"},
            default_group="visitorClass",
            required=("visitorClass",),
            daily=True,
            limits=("통신 자료로 만든 추정치다.", "일자별 순방문자의 합이라 2박 3일이면 3명으로 세어져 실제 사람 수가 아니다."),
            note="제주 전체 일별 방문자. visitorClass(현지인/외지인/외국인) 필수",
        ),
        Metric(
            "visitors_city", "시별 방문자 추정치", "명(일별 합계)", "tour_local_daily", "baseDate", "visitors", "tour_local",
            {"sigungu": "sigungu", "visitorClass": "visitorClass"},
            default_group="sigungu",
            required=("visitorClass",),
            daily=True,
            limits=("통신 자료로 만든 추정치다.", "일자별 순방문자의 합이라 2박 3일이면 3명으로 세어져 실제 사람 수가 아니다.", "제주시와 서귀포시를 더해 제주 전체로 쓰지 않는다. 제주 전체는 visitors_jeju 를 쓴다."),
            note="제주시·서귀포시 일별 방문자. visitorClass 필수. 두 시를 더하지 않고 항상 시별로 나눠 돌려준다",
        ),
        Metric(
            "bus_station", "정류소별 버스 이용자", "명", "bus_station_daily", "baseDate", "userCnt", "bus_daily",
            {"name": "nameKey", "stationId": "stationId", "moveType": "moveType", "priceType": "priceType"},
            default_group="name",
            defaults={"moveType": "승차"},
            where="NOT isPartialDay",
            daily=True,
            partial_days_ok=True,
            limits=("원본 일부 누락으로 보이는 날은 합계에서 뺐다. 제공처에 확인한 것은 아니다.", "다른 버스 자료와 인원 정의가 달라 더하거나 비교하지 않는다."),
            note="최신 정류소별 버스 승차·환승. 다른 버스 자료와 비교 불가",
        ),
        Metric(
            "card_industry", "업종별 카드 이용금액", "원", "card_industry_monthly", "dtYearMonth", "useAmount", "card_industry_monthly",
            {"industry": "industry", "spenderGroup": "spenderGroup", "userType": "userType", "sex": "sex"},
            default_group="industry",
            defaults={"spenderGroup": "관광객(추정)"},
            limits=(
                "특정 카드 자료로 보이며 제주 전체 소비액이 아니다. 어느 카드사 자료인지 알 수 없고 외국 카드 대부분이 빠졌을 수 있다.",
                "관광객(추정)은 내국인과 외국인 유형을 더한 값이다. 내국인을 도민이 아닌 내국인으로 추정했을 뿐 제공처 정의가 없고, 관광 목적인지 알 수 없다.",
                "금액이 줄거나 늘어도 소비 변화인지 카드 집계 범위 변화인지 알 수 없어 원인을 단정하지 않는다.",
                "이용자 수를 더할 수 없어 1인당 금액은 계산하지 않는다.",
            ),
            note="업종별 소비. 기본은 관광객(추정)이고 spenderGroup 으로 제주도민, 법인을 고른다. 나라별은 userType",
        ),
        Metric(
            "card_time", "시간대별 카드 이용금액", "원", "card_time_monthly", "dtYearMonth", "useAmount", "card_time_monthly",
            {"timeSlot": "timeSlot", "spenderGroup": "spenderGroup", "userType": "userType", "ageRange": "ageRange"},
            default_group="timeSlot",
            defaults={"spenderGroup": "관광객(추정)"},
            limits=(
                "특정 카드 자료로 보이며 제주 전체 소비액이 아니다. 어느 카드사 자료인지 알 수 없고 외국 카드 대부분이 빠졌을 수 있다.",
                "관광객(추정)은 내국인과 외국인 유형을 더한 값이다. 내국인을 도민이 아닌 내국인으로 추정했을 뿐 제공처 정의가 없고, 관광 목적인지 알 수 없다.",
                "금액이 줄거나 늘어도 소비 변화인지 카드 집계 범위 변화인지 알 수 없어 원인을 단정하지 않는다.",
                "이용자 수를 더할 수 없어 1인당 금액은 계산하지 않는다.",
            ) + ("시간대 구간의 정확한 시각은 자료에 없다.", "원본이 백만 원 단위라 업종별 합계보다 조금 작다."),
            note="시간대(오전, 점심, 오후, 저녁, 밤)별 소비. 기본은 관광객(추정)이고 spenderGroup 으로 제주도민, 법인을 고른다",
        ),
        Metric(
            "sbiz_bsi", "소상공인 경기지수(BSI)", "지수", "sbiz_bsi_monthly", "dtYearMonth", "bsi", "sbiz_bsi_monthly",
            {"region": "region", "outlook": "outlook"},
            default_group="region",
            defaults={"region": "제주", "outlook": "체감"},
            agg="mean",
            limits=(
                "매출액이나 관광객 수가 아니라 소상공인 응답으로 만든 지수다. 100보다 크면 좋아졌다(좋아질 것)는 응답이 많다는 뜻이다.",
                "지역 값은 업종을 나누지 않아 관광 업종만 따로 볼 수 없다.",
                "연·기간 값은 월 지수의 평균이다. 증감은 % 가 아니라 포인트 차이(changePoints, yoyPoints)로 말한다.",
                "제주 체감 지수는 2014-10~2015-05와 2025-06, 전망 지수는 2014-11~2015-05가 비어 있다.",
                "지역별 표본 수와 체감·전망의 정확한 정의는 파일에 없다.",
            ),
            note="소상공인 체감·전망 경기지수. 기본은 제주 체감이고 region(전국, 시도)과 outlook(체감/전망)을 고른다. 여러 달은 평균한다",
        ),
        Metric(
            "restaurant_open", "음식점 인허가 건수", "곳", "restaurant_monthly", "dtYearMonth", "cnt", "restaurants",
            {"sigungu": "sigungu", "area": "area", "foodGroup": "foodGroup", "category": "category", "businessType": "businessType"},
            default_group="foodGroup",
            where="eventType = '인허가'",
            fill_zero=True,
            limits=(
                "새로 문을 연 가게만이 아니라 영업자가 바뀌어 새로 인허가를 받은 경우가 섞였을 수 있으나 확인하지 못했다.",
                "2025-09-02 갱신 목록에서 센 값이다. 2010년부터 세고, 2025년은 1~8월만 있다.",
                "관광객 수요와 도민 수요를 나눌 수 없다. 업종 묶음(foodGroup)은 이 프로젝트가 원본 소분류를 묶은 것이다.",
                "영업 중인 업소 수는 이 지표로 계산하지 않는다. 2025-09-02 기준 영업 중 20,954곳, 폐업 30,156곳이다.",
            ),
            note="달별 음식점 인허가(개업) 건수. 업종 묶음(foodGroup: 카페, 한식, 주점 등), 읍면동(area), 시군 필터",
        ),
        Metric(
            "restaurant_close", "음식점 폐업 건수", "곳", "restaurant_monthly", "dtYearMonth", "cnt", "restaurants",
            {"sigungu": "sigungu", "area": "area", "foodGroup": "foodGroup", "category": "category", "businessType": "businessType"},
            default_group="foodGroup",
            where="eventType = '폐업'",
            fill_zero=True,
            limits=(
                "폐업일 기준 건수다. 1994년 전 폐업 기록이 없어 오래전 폐업은 빠졌을 수 있어 2010년부터 센다. 2025년은 1~8월만 있다.",
                "관광객 수요와 도민 수요를 나눌 수 없다. 업종 묶음(foodGroup)은 이 프로젝트가 원본 소분류를 묶은 것이다.",
                "폐업 건수가 늘어도 그 업종 업소 수가 늘어서인지 장사가 어려워서인지 이 자료로 나눌 수 없다.",
                "영업 중인 업소 수는 이 지표로 계산하지 않는다. 2025-09-02 기준 영업 중 20,954곳, 폐업 30,156곳이다.",
            ),
            note="달별 음식점 폐업 건수. 필터는 restaurant_open 과 같다",
        ),
    ]
}


@lru_cache(maxsize=None)
def distinct_values(metric_id: str, dim: str) -> tuple[str, ...]:
    m = METRICS[metric_id]
    where = f"WHERE {m.where}" if m.where else ""
    rows = query(f"SELECT DISTINCT {m.dims[dim]} AS v FROM {m.source} {where} ORDER BY 1")
    return tuple(str(v) for v in rows["v"].dropna())


def needs_clarification(message: str, candidates: list) -> ToolResult:
    return ToolResult("needs_clarification", message, summary={"candidates": list(candidates)})


def resolve_spot_name(name: str) -> tuple[str | None, list[str]]:
    """별칭까지 찾아 관광지 이름을 정한다. (정해진 이름, 후보들)을 돌려준다."""
    text = name.strip()
    exact = query("SELECT DISTINCT spotName FROM spot_alias WHERE alias = ? UNION SELECT spotName FROM dim_spot WHERE spotName = ?", [text, text])
    if len(exact) == 1:
        return exact["spotName"][0], []
    squeezed = text.replace(" ", "")
    partial = query(
        "SELECT DISTINCT spotName FROM spot_alias WHERE replace(alias, ' ', '') LIKE '%' || ? || '%' OR ? LIKE '%' || replace(alias, ' ', '') || '%' ORDER BY 1",
        [squeezed, squeezed],
    )
    names = list(partial["spotName"])
    return (names[0], []) if len(names) == 1 else (None, names)


def normalize_filters(metric: Metric, filters: dict | None, group_by: str | None = None) -> tuple[dict, ToolResult | None]:
    """필터를 확인해 DB 값으로 바꾼다. 필요한 값이 없거나 모호하면 되묻는 결과를 돌려준다."""
    clean = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    for key in clean:
        if key not in metric.dims:
            return {}, ToolResult("invalid_request", f"{metric.id} 에 쓸 수 없는 필터입니다: {key}. 가능한 필터: {', '.join(metric.dims)}")

    if "spot" in clean:
        spot, candidates = resolve_spot_name(clean["spot"])
        if spot is None and candidates:
            return {}, needs_clarification(f"'{clean['spot']}' 에 해당하는 관광지가 여럿입니다.", candidates)
        if spot is None:
            return {}, ToolResult("not_found", f"'{clean['spot']}' 관광지는 입장객 자료에 없습니다.")
        clean["spot"] = spot
    if "sigungu" in clean:
        clean["sigungu"] = SIGUNGU_NAMES.get(clean["sigungu"], clean["sigungu"])
    # "카페" 처럼 업종 묶음 이름을 원본 소분류로 넘기면 묶음으로 찾는다
    if "category" in clean and "foodGroup" in metric.dims and "foodGroup" not in clean and clean["category"] in distinct_values(metric.id, "foodGroup"):
        clean["foodGroup"] = clean.pop("category")

    for key, value in list(clean.items()):
        if key in ("spot", "name", "stationId"):
            continue
        choices = distinct_values(metric.id, key)
        if value in choices:
            continue
        near = [c for c in choices if value in c or c in value]
        if len(near) != 1:
            return {}, needs_clarification(f"{key} 값 '{value}' 을 찾지 못했습니다. 다음 중에서 골라 주세요.", near or choices)
        clean[key] = near[0]

    if metric.id == "visit_purpose":
        if "purpose" in clean:
            clean["classType"] = query("SELECT any_value(classType) AS c FROM visit_purpose WHERE visitPurpose = ?", [clean["purpose"]])["c"][0]
        elif "classType" not in clean:
            return {}, needs_clarification("방문목적과 여행형태는 같은 사람을 나눈 값이라 하나를 골라야 합니다.", distinct_values(metric.id, "classType"))
    for key in metric.required:
        if key not in clean:
            return {}, needs_clarification(f"{metric.label} 은 {key} 를 골라야 합니다.", distinct_values(metric.id, key))
    for key, value in metric.defaults.items():
        # 이 기준으로 나눠 보거나 더 좁은 유형을 골랐으면 기본값으로 좁히지 않는다
        if key != group_by and not (key == "spenderGroup" and "userType" in clean):
            clean.setdefault(key, value)
    return clean, None


def fetch(metric: Metric, filters: dict, group_cols: tuple[str, ...] = ()) -> pd.DataFrame:
    """필터를 적용해 달과 묶음별로 더한 표를 돌려준다. 값은 value, 자료가 있는 날 수는 days 다."""
    where = [metric.where] if metric.where else []
    params = []
    for key, value in filters.items():
        where.append(f"{metric.dims[key]} = ?")
        params.append(value)
    selects = [f"date_trunc('month', {metric.time_col})::DATE AS month"]
    selects += [f"{metric.dims[c]} AS {c}" for c in group_cols]
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    df = query(
        f"SELECT {', '.join(selects)}, sum({metric.value_col})::DOUBLE AS value, count(DISTINCT {metric.time_col}) AS days "
        f"FROM {metric.source} {where_sql} GROUP BY ALL ORDER BY month",
        params,
    )
    df["month"] = pd.PeriodIndex(pd.to_datetime(df["month"]), freq="M")
    return _fill_months(metric, df, group_cols) if metric.fill_zero and not df.empty else df


@lru_cache(maxsize=None)
def coverage_months(metric_id: str) -> tuple[pd.Period, pd.Period]:
    """필터와 관계없이 지표 전체의 첫 달과 마지막 달."""
    m = METRICS[metric_id]
    where = f"WHERE {m.where}" if m.where else ""
    row = query(f"SELECT min({m.time_col}) AS s, max({m.time_col}) AS e FROM {m.source} {where}").iloc[0]
    return pd.Period(row["s"], freq="M"), pd.Period(row["e"], freq="M")


def _fill_months(metric: Metric, df: pd.DataFrame, group_cols: tuple[str, ...]) -> pd.DataFrame:
    """건수 지표는 행이 없는 달이 0건이다. 제공 기간의 모든 달을 묶음마다 채운다."""
    start, end = coverage_months(metric.id)
    grid = pd.DataFrame({"month": pd.period_range(start, end, freq="M")})
    if group_cols:
        grid = grid.merge(df[list(group_cols)].drop_duplicates(), how="cross")
    keys = ["month", *group_cols]
    out = grid.merge(df, on=keys, how="left").fillna({"value": 0.0, "days": 0})
    out["days"] = out["days"].astype(int)
    return out


def fetch_daily(metric: Metric, filters: dict) -> pd.DataFrame:
    """일별 자료를 날짜별로 더하고 달력의 요일과 공휴일을 붙인다."""
    where = [metric.where] if metric.where else []
    params = []
    for key, value in filters.items():
        where.append(f"{metric.dims[key]} = ?")
        params.append(value)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    df = query(
        f"SELECT v.day, v.value, d.dayOfWeek, d.isWeekend, d.isOffDay FROM "
        f"(SELECT {metric.time_col} AS day, sum({metric.value_col})::DOUBLE AS value FROM {metric.source} {where_sql} GROUP BY 1) v "
        f"JOIN dim_date d ON d.baseDate = v.day ORDER BY v.day",
        params,
    )
    df["month"] = pd.PeriodIndex(pd.to_datetime(df["day"]), freq="M")
    return df


def month_is_complete(metric: Metric, frame: pd.DataFrame) -> pd.Series:
    """그 달 값을 온전한 값으로 쓸 수 있는지. 일별 자료는 그 달의 날이 다 있어야 한다."""
    if metric.fill_zero:
        return pd.Series(True, index=frame.index)
    if metric.partial_days_ok:
        return frame["value"] > 0
    if metric.daily:
        return frame["days"] == frame["month"].dt.days_in_month
    return frame["value"] > 0


def filter_period(df: pd.DataFrame, start: pd.Period | None, end: pd.Period | None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if start is not None:
        mask &= df["month"] >= start
    if end is not None:
        mask &= df["month"] <= end
    return df[mask]


def crosses_break(metric: Metric, start: pd.Period, end: pd.Period) -> bool:
    if metric.break_month is None:
        return False
    return start < pd.Period(metric.break_month, freq="M") <= end


def catalog_limits(metric: Metric) -> list[str]:
    return list(metric.limits)


def bus_partial_days(start: pd.Period, end: pd.Period) -> list[str]:
    rows = query(
        "SELECT DISTINCT strftime(baseDate, '%Y-%m-%d') AS d FROM bus_station_daily WHERE isPartialDay AND baseDate BETWEEN ? AND ? ORDER BY 1",
        [start.start_time.date(), end.end_time.date()],
    )
    return list(rows["d"])


def metrics_for_prompt() -> str:
    """시스템 프롬프트에 넣을 지표 설명. 순서가 고정되어 캐시를 깨지 않는다."""
    lines = []
    for m in METRICS.values():
        filters = ", ".join(m.dims)
        lines.append(f"- {m.id}: {m.label}. {m.note}. 필터: {filters}")
    return "\n".join(lines)
