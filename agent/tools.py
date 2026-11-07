"""분석 도구 10개. LLM은 SQL을 쓰지 않고 이 함수와 조건만 고른다. 모든 수치는 여기서 계산한다."""

from __future__ import annotations

import re
from datetime import date as Date

import pandas as pd

from agent.common import ToolResult, invalid, month_range_text, month_text, parse_month, pct_change, query, ratio_pct
from agent.metrics import (
    METRICS,
    Metric,
    bus_partial_days,
    catalog_limits,
    crosses_break,
    fetch,
    fetch_daily,
    filter_period,
    month_is_complete,
    normalize_filters,
    resolve_spot_name,
)

STOPWORDS = {"데이터", "자료", "관련", "관련된", "뭐", "무엇", "어떤", "있어", "있나요", "볼", "수", "알려줘", "보고", "싶어"}
# 거의 모든 자료에 들어 있는 말. 이 말만으로 찾으면 묻지 않은 자료가 찾은 것처럼 나온다
GENERIC_TOKENS = {"관광", "관광객", "제주", "제주도", "정보", "현황", "통계", "이용", "비율", "얼마나", "많이"}
JOSA = re.compile(r"(이랑|에서|으로|까지|부터|이나|도|는|은|이|가|을|를|의|로|에|랑|와|과)$")
COVID_YEARS = (2020, 2021)
# 이보다 날 수가 적은 구간은 값을 말하지 않는다
MIN_WEATHER_DAYS = 20
# 평일이나 주말의 날 수가 이보다 적으면 차이를 말하지 않는다
MIN_DAY_TYPE_DAYS = 8
DAYS_OF_WEEK = ["월", "화", "수", "목", "금", "토", "일"]
# 기준값이 중앙값의 이 비율보다 작으면 비율이 크게 나온다고 표시한다
SMALL_BASE_RATIO = 0.25
# 대상이 적으면(내국인·외국인 둘 등) 중앙값이 기준 구실을 못 해 작은 기준 표시를 하지 않는다
SMALL_BASE_MIN_GROUPS = 5
HUB_SEARCH_URL = "https://jejudatahub.net/data/list"
HUB_SEARCH_GUIDE = f"이 프로그램에 없는 자료가 제주데이터허브에 있는지는 알 수 없다. 있다거나 없다고 짐작하지 말고, 허브 웹사이트({HUB_SEARCH_URL})에서 직접 검색하도록 안내한다."


def _tokens(text: str) -> list[str]:
    words = [JOSA.sub("", w) for w in re.split(r"[\s,/·?!.]+", text.strip()) if w]
    return [w for w in words if w and w not in STOPWORDS]


def _metric_or_error(metric_id: str) -> Metric | ToolResult:
    metric = METRICS.get(metric_id)
    return metric or invalid(f"없는 지표입니다: {metric_id}. 가능한 지표: {', '.join(METRICS)}")


def _period_args(start: str | None, end: str | None) -> tuple[pd.Period | None, pd.Period | None]:
    return (parse_month(start) if start else None, parse_month(end, is_end=True) if end else None)


def _evidence(metric: Metric, period: str, row_count: int, method: str, **extra) -> dict:
    return {"metric": metric.id, "label": metric.label, "unit": metric.unit, "catalogId": metric.catalog_id, "period": period, "rowCount": row_count, "method": method, **extra}


def _coverage_text(df: pd.DataFrame) -> str:
    return month_range_text(df["month"].min(), df["month"].max())


def _total(metric: Metric, values: pd.Series) -> float:
    """여러 달·대상을 하나로 합친 값. 지수는 더하지 않고 평균한다."""
    return round(values.mean(), 1) if metric.agg == "mean" else values.sum()


def _uses_points(metric: Metric) -> bool:
    """지수와 비율(%)은 % 증감보다 포인트 차이로 말한다. 등록대수나 1인당 금액 같은 평균값은 아니다."""
    return metric.agg == "mean" and metric.unit in ("지수", "%")


def _points(metric: Metric, new: float | None, base: float | None) -> dict:
    """지수는 % 보다 포인트 차이로 말한다."""
    if not _uses_points(metric) or new is None or base is None:
        return {}
    return {"changePoints": round(new - base, 1)}


def _total_word(metric: Metric, particle: str = "") -> str:
    word = "평균" if metric.agg == "mean" else "합계"
    if particle == "로":
        return word + ("으로" if metric.agg == "mean" else "로")
    return word


def _scope(metric: Metric, filters: dict) -> dict:
    return {"filters": filters, **({"visitorScope": "내국인+외국인 합산"} if metric.breakdown and "visitorType" not in filters else {})}


def _aggregate_years(metric: Metric, df: pd.DataFrame, group: str | None) -> pd.DataFrame:
    """달을 연으로 묶는다. 12개월이 온전한 해만 isComplete 이다."""
    frame = df.assign(year=df["month"].dt.year, ok=month_is_complete(metric, df))
    keys = ["year"] + ([group] if group else [])
    if metric.id == "spot_visitors":
        # 관광지 수가 달라지는지 보려면 달마다 묶기 전 값이 필요하다
        monthly = frame.groupby(keys + ["month"], as_index=False).agg(value=("value", "sum"))
        monthly["ok"] = monthly["value"] > 0
    else:
        monthly = frame.groupby(keys + ["month"], as_index=False).agg(value=("value", "sum"), ok=("ok", "min"))
    out = monthly.groupby(keys, as_index=False).agg(value=("value", metric.agg), months=("ok", "sum"))
    out["isComplete"] = out["months"] == 12
    return out


def _spot_sets(df: pd.DataFrame, key: str) -> dict:
    live = df[df["value"] > 0]
    return live.groupby(key)["spot"].agg(frozenset).to_dict()


def _recent_12(metric: Metric, df: pd.DataFrame) -> dict | None:
    """가장 최근 12개월과 그 전 12개월을 비교한다. 일부 달만 있는 연도를 피하려는 값이다."""
    frame = df.assign(ok=month_is_complete(metric, df))
    if "spot" in df:
        sets = _spot_sets(df, "month")
    monthly = frame.groupby("month", as_index=False).agg(value=("value", "sum"), ok=("ok", "max" if "spot" in df else "min")).sort_values("month")
    monthly = monthly[monthly["ok"]]
    if len(monthly) < 24:
        return None
    last = monthly.tail(24)
    expected = pd.period_range(last["month"].iloc[0], last["month"].iloc[-1], freq="M")
    if len(expected) != 24 or not all(last["month"].values == expected):
        return None
    if "spot" in df and len({sets[m] for m in last["month"]}) > 1:
        return None
    prior, recent = last.head(12), last.tail(12)
    recent_value, prior_value = _total(metric, recent["value"]), _total(metric, prior["value"])
    return {
        "recentPeriod": month_range_text(recent["month"].iloc[0], recent["month"].iloc[-1]),
        "recentValue": recent_value,
        "priorPeriod": month_range_text(prior["month"].iloc[0], prior["month"].iloc[-1]),
        "priorValue": prior_value,
        "changePct": pct_change(recent_value, prior_value),
        **_points(metric, recent_value, prior_value),
    }


def _breakdown_by_year(metric: Metric, filters: dict) -> dict[int, dict]:
    df = fetch(metric, filters, ("visitorType",))
    df["year"] = df["month"].dt.year
    grouped = df.groupby(["year", "visitorType"])["value"].sum().unstack(fill_value=0)
    return {year: row.to_dict() for year, row in grouped.iterrows()}


def _nearest_complete_year(metric: Metric, df: pd.DataFrame, requested_after: bool) -> tuple[int | None, float | None]:
    years = _aggregate_years(metric, df, None)
    complete = years[years["isComplete"]].sort_values("year")
    if complete.empty:
        return None, None
    row = complete.iloc[-1] if requested_after else complete.iloc[0]
    return int(row["year"]), row["value"]


def _out_of_range(metric: Metric, df: pd.DataFrame, filters: dict, start: pd.Period | None, end: pd.Period | None, group: str | None = None) -> ToolResult:
    coverage = _coverage_text(df)
    requested_after = bool(start is not None and start > df["month"].max())
    year, value = _nearest_complete_year(metric, df, requested_after)
    requested = month_range_text(start or end, end or start)
    rows = [{"period": str(year), "value": value, "isComplete": True}] if year else []
    if year and group:
        by_group = df[df["month"].dt.year == year].groupby(group)["value"].agg(metric.agg)
        rows = [{"period": str(year), group: name, "value": v, "isComplete": True} for name, v in by_group.items()]
    limits = [f"{metric.label} 자료는 {coverage} 만 있다. 없는 기간의 값은 0이 아니라 자료 없음이다."] + catalog_limits(metric)
    message = f"요청 기간 {requested} 에는 자료가 없습니다. 제공 기간은 {coverage} 입니다."
    if year:
        message += f" 가장 가까운 온전한 해는 {year}년이고 값은 rows 에 있습니다. 요청 기간의 값으로 말하지 마세요."
    return ToolResult(
        "out_of_range",
        message,
        rows=rows,
        summary={"availablePeriod": coverage, "nearestYear": year},
        evidence=_evidence(metric, coverage, len(rows), f"요청 기간에 자료가 없어 가장 가까운 온전한 해의 {_total_word(metric)}", **_scope(metric, filters)),
        limits=limits,
    )


def _not_comparable(metric: Metric, reason: str, filters: dict, coverage: str = "") -> ToolResult:
    return ToolResult(
        "not_comparable",
        reason,
        summary={"availablePeriod": coverage} if coverage else {},
        evidence=_evidence(metric, coverage, 0, "비교하지 않음", **_scope(metric, filters)),
        limits=catalog_limits(metric),
        alternatives=list(metric.alternatives),
    )


def _regime_message(metric: Metric) -> str:
    return f"{metric.label} 은 {metric.break_month} 에 집계 방식이 달라진 것으로 보여 이 시점을 넘어 비교할 수 없습니다. {metric.break_month} 이전과 이후를 나눠서 물어 주세요."


def _city_comparison(filters: dict, start: pd.Period, end: pd.Period) -> dict | None:
    """제주 전체 값과 두 시 합계의 배율. 두 시를 더하면 안 되는 이유를 수치로 보여 준다."""
    params = [filters["visitorClass"], start.start_time.date(), end.end_time.date()]
    condition = "visitorClass = ? AND baseDate BETWEEN ? AND ?"
    jeju = query(f"SELECT sum(visitors) AS v FROM tour_metro_daily WHERE {condition}", params)["v"][0]
    cities = query(f"SELECT sum(visitors) AS v FROM tour_local_daily WHERE {condition}", params)["v"][0]
    if pd.isna(jeju) or pd.isna(cities) or not jeju:
        return None
    return {"period": month_range_text(start, end), "jejuTotal": round(jeju), "twoCitySum": round(cities), "twoCitySumToJejuRatio": round(cities / jeju, 2)}


def find_data(query_text: str = "") -> ToolResult:
    """F1. 질문 키워드로 쓸 수 있는 데이터와 센터 전용 명세를 찾는다."""
    tokens = _tokens(query_text)
    tokens = [t for t in tokens if t not in GENERIC_TOKENS] or tokens
    catalog = query("SELECT * FROM data_catalog ORDER BY id")
    fields = ["title", "keywords", "description", "questions"]
    catalog["searchText"] = catalog[fields].fillna("").agg(" ".join, axis=1)
    catalog["score"] = catalog["searchText"].map(lambda t: sum(tok in t for tok in tokens))

    usable = catalog[catalog["status"] == "사용"]
    if tokens:
        usable = usable[usable["score"] > 0]
    usable = usable.sort_values(["score", "id"], ascending=[False, True])
    rows = [
        {"id": r["id"], "title": r["title"], "topic": r["features"], "grain": r["grain"], "startDate": r["startDate"], "endDate": r["endDate"], "description": r["description"], "limits": r["limits"]}
        for r in usable.to_dict("records")
    ]

    center = query("SELECT centerNo, category, title, exportable, provider, description FROM center_datasets ORDER BY centerNo")
    center_rows = []
    if tokens:
        text = center[["category", "title", "description"]].fillna("").agg(" ".join, axis=1)
        center["score"] = text.map(lambda t: sum(tok in t for tok in tokens))
        center_rows = center[center["score"] > 0].sort_values(["score", "centerNo"], ascending=[False, True]).drop(columns="score").to_dict("records")

    usable_text = " ".join(catalog.loc[catalog["status"] == "사용", "searchText"])
    only_in_center = [tok for tok in tokens if tok not in usable_text and any(tok in f"{c['title']} {c['description']}" for c in center_rows)]
    # 낱말이 서로 다른 자료에 하나씩 있으면 함께 볼 수 없다 (예: 읍면동은 음식점, 카드는 카드 자료)
    together = len(tokens) > 1 and not (catalog.loc[catalog["status"] == "사용", "score"] == len(tokens)).any()
    combined = not only_in_center and together and any(all(tok in f"{c['category']} {c['title']} {c['description']}" for tok in tokens) for c in center_rows)
    if combined:
        only_in_center = list(tokens)
    status = "center_only" if only_in_center else "ok"
    center_text = " ".join(f"{c['title']} {c['description']}" for c in center_rows)
    not_found = [tok for tok in tokens if tok not in usable_text and tok not in center_text]

    excluded = catalog[(catalog["status"] == "제외") & (catalog["score"] > 0)]
    limits = ["센터 전용 데이터는 명세만 있어 값을 계산할 수 없다."]
    summary = {
        # 대회 제공 허브 목록은 제3자 제공이 금지되어 화면에도 보여 주지 않는다. 허브 웹사이트 검색만 안내한다
        "hubSearch": HUB_SEARCH_GUIDE,
        "usableCount": len(rows),
        "centerMatchCount": len(center_rows),
        "centerMatches": center_rows[:10],
        "excluded": [{"title": r["title"], "reason": r["limits"]} for r in excluded.to_dict("records")],
        "tokensOnlyInCenter": only_in_center,
        "tokensNotFound": not_found,
    }

    message = ""
    if combined:
        message = f"'{', '.join(tokens)}' 를 함께 담은 값은 분석 가능한 자료에 없고 제주빅데이터센터 전용 명세에만 있습니다. 낱말마다 다른 자료에서 찾은 rows 를 합쳐 답하지 마세요. 값을 계산할 수 없고 명세만 안내할 수 있습니다."
    elif only_in_center:
        message = f"'{', '.join(only_in_center)}' 관련 값은 분석 가능한 자료에 없고 제주빅데이터센터 전용 명세에만 있습니다. 값을 계산할 수 없고 명세만 안내할 수 있습니다."
    elif not rows:
        message = "분석 가능한 자료에서 찾지 못했습니다."
    elif not_found:
        message = f"'{', '.join(not_found)}' 에 관한 값은 어떤 자료에도 없습니다. rows 는 나머지 낱말로 찾은 자료이므로 그 값으로 답하지 마세요."
    return ToolResult(
        status,
        message,
        rows=rows,
        summary=summary,
        evidence={"table": "data_catalog, center_datasets", "rowCount": len(rows), "method": "키워드가 제목·키워드·설명·예시 질문에 들어 있는 항목 검색"},
        limits=limits,
    )


def _center_download(center_no: str) -> dict:
    """CSV를 넣어 분석할 수 있는 센터 자료는 내려받는 방법을 함께 알린다."""
    from agent.center import SPECS

    spec = SPECS.get(center_no)
    if spec is None:
        return {}
    return {"alternatives": [{"tool": "analyze_center_csv", "dataset": center_no, "download": spec.download, "note": "센터에서 이 자료의 CSV를 내려받아 넣으면 analyze_center_csv 로 계산할 수 있다."}]}


def describe_data(data_id: str) -> ToolResult:
    """F1. 데이터의 항목과 기간을 DB에서 읽어 설명한다. id 는 카탈로그 id 또는 센터 번호(1-01)다."""
    center = query("SELECT * FROM center_datasets WHERE centerNo = ?", [data_id])
    if len(center):
        columns = query("SELECT columnName, columnNameKo, dataType FROM center_columns WHERE centerNo = ? ORDER BY ordinal", [data_id])
        info = center.iloc[0]
        return ToolResult(
            "center_only",
            f"'{info['title']}' 은 제주빅데이터센터 전용 명세입니다. 값이 없어 계산할 수 없고 항목만 안내할 수 있습니다.",
            rows=columns.to_dict("records"),
            summary={"title": info["title"], "category": info["category"], "provider": info["provider"], "exportable": bool(info["exportable"]), "description": info["description"]},
            evidence={"table": "center_datasets, center_columns", "rowCount": len(columns), "method": "센터 명세 v13 조회"},
            limits=["명세만 있고 데이터 자체는 없다.", "명세에 제공 기간 항목이 없다. 등록일은 데이터 기간이 아니다."],
            **_center_download(data_id),
        )
    # LLM이 지표 ID를 넘겨도 찾을 수 있게 한다
    catalog_id = METRICS[data_id].catalog_id if data_id in METRICS else data_id
    found = query("SELECT * FROM data_catalog WHERE id = ?", [catalog_id])
    if found.empty:
        return ToolResult("not_found", f"'{data_id}' 데이터를 찾지 못했습니다. find_data 로 id 를 먼저 찾으세요.")
    info = found.iloc[0]
    columns = query("SELECT columnName, columnType, description FROM data_columns WHERE tableName = ?", [info["tableName"]]) if pd.notna(info["tableName"]) else pd.DataFrame()
    status = {"사용": "ok", "센터 전용": "center_only"}.get(info["status"], "not_comparable")
    message = "" if status == "ok" else f"'{info['title']}' 은 상태가 '{info['status']}' 라 값을 계산할 수 없습니다."
    return ToolResult(
        status,
        message,
        rows=columns.to_dict("records"),
        summary={k: info[k] for k in ("title", "status", "provider", "license", "grain", "startDate", "endDate", "rowCount", "description")},
        evidence={"table": "data_catalog, data_columns", "rowCount": len(columns), "method": "카탈로그 조회"},
        limits=[info["limits"]] if pd.notna(info["limits"]) else [],
    )


def resolve_spot(name: str) -> ToolResult:
    """관광지 이름을 확인한다. 여럿이면 후보를 돌려줘 되묻게 하고 임의로 고르지 않는다."""
    spot, candidates = resolve_spot_name(name)
    evidence = {"table": "dim_spot, spot_alias", "method": "이름과 별칭 대조"}
    if spot is None and candidates:
        return ToolResult("needs_clarification", f"'{name}' 에 해당하는 관광지가 {len(candidates)}곳입니다.", summary={"candidates": candidates}, evidence=evidence)
    if spot is None:
        return ToolResult("not_found", f"'{name}' 은 입장객 자료가 있는 관광지 50곳에 없습니다.", evidence=evidence, limits=["입장 집계가 있는 관광지만 자료가 있다."])
    info = query("SELECT spotName, sigungu, category, firstMonth, lastMonth, sigunguConfidence FROM dim_spot WHERE spotName = ?", [spot]).iloc[0]
    row = {"spotName": info["spotName"], "sigungu": info["sigungu"], "category": info["category"], "firstMonth": info["firstMonth"].strftime("%Y-%m"), "lastMonth": info["lastMonth"].strftime("%Y-%m")}
    limits = ["이 관광지의 시군 구분은 근거가 약하다."] if info["sigunguConfidence"] == "낮음" else []
    return ToolResult("ok", rows=[row], evidence=evidence, limits=limits)


def get_trend(metric: str, filters: dict | None = None, start: str | None = None, end: str | None = None, grain: str = "year", group_by: str | None = None) -> ToolResult:
    """F2. 기간별 추세. 일부 달만 있는 해는 온전한 해와 증감을 계산하지 않는다."""
    m = _metric_or_error(metric)
    if isinstance(m, ToolResult):
        return m
    if grain not in ("year", "month"):
        return invalid("grain 은 year 또는 month 입니다.")
    clean, problem = normalize_filters(m, filters, group_by)
    if problem:
        return problem
    if group_by and group_by not in m.dims:
        return invalid(f"{m.id} 은 {', '.join(m.dims)} 로만 묶을 수 있습니다.")
    if m.id == "visitors_city" and "sigungu" not in clean:
        group_by = "sigungu"
    p_start, p_end = _period_args(start, end)

    cols = tuple(c for c in [group_by, "spot" if m.id == "spot_visitors" else None] if c)
    cols = tuple(dict.fromkeys(cols))
    full = fetch(m, clean, cols)
    if full.empty:
        return ToolResult("not_found", "조건에 맞는 자료가 없습니다.", evidence=_evidence(m, "", 0, "조회", **_scope(m, clean)))
    coverage = _coverage_text(full)
    if m.break_month:
        if p_start is None and p_end is None:
            p_start = pd.Period(m.break_month, freq="M")
        elif crosses_break(m, p_start if p_start is not None else full["month"].min(), p_end if p_end is not None else full["month"].max()):
            return _not_comparable(m, _regime_message(m), clean, coverage)
    df = filter_period(full, p_start, p_end)
    if df.empty:
        return _out_of_range(m, full, clean, p_start, p_end, group_by)

    limits = catalog_limits(m)
    used = _coverage_text(df)
    if (p_start is not None and p_start < full["month"].min()) or (p_end is not None and p_end > full["month"].max()):
        limits.insert(0, f"요청 기간 중 자료가 있는 {used} 만 계산했다. 제공 기간은 {coverage} 다.")

    rows = _trend_rows(m, df, grain, group_by)
    partial_years = sorted({r["period"] for r in rows if grain == "year" and not r["isComplete"]})
    if partial_years:
        limits.append(f"{', '.join(partial_years)}년은 일부 달만 있어 연간 합계를 다른 해와 비교하지 않는다.")
    summary: dict = {"availablePeriod": coverage}
    if not group_by:
        recent = _recent_12(m, full)
        if recent:
            summary["recent12Months"] = recent
    if group_by is None and grain == "year" and m.breakdown and "visitorType" not in clean:
        breakdown = _breakdown_by_year(m, clean)
        for row in rows:
            row["breakdown"] = breakdown.get(int(row["period"]), {})
    if m.id == "spot_visitors" and "spot" in df and "spot" not in clean:
        if len({len(s) for s in _spot_sets(df, "month").values()}) > 1:
            limits.append("기간에 따라 자료가 있는 관광지 수가 달라 합계를 그대로 비교하면 왜곡될 수 있다. rows 의 spotCount 를 함께 본다.")
    if m.id in ("visitors_jeju", "visitors_city"):
        span_start, span_end = df["month"].min(), df["month"].max()
        comparison = _city_comparison(clean, span_start, span_end)
        if comparison:
            summary["cityComparison"] = comparison
            limits.append(f"두 시 합계({comparison['twoCitySum']:,}명)는 제주 전체({comparison['jejuTotal']:,}명)의 {comparison['twoCitySumToJejuRatio']}배라 더해서 제주 전체로 쓰지 않는다. 두 시를 모두 간 사람이 두 번 세어지는 것으로 보이나 확인되지 않았다.")
    if m.partial_days_ok:
        excluded = bus_partial_days(df["month"].min(), df["month"].max())
        summary["excludedDays"] = excluded
        summary["daysUsed"] = int(df.groupby("month")["days"].max().sum())

    series = group_by if group_by else None
    chart = {"type": "line" if grain == "month" else "bar", "x": "period", "y": "value", "series": series, "title": f"{m.label} 추세"}
    # 요청한 앞 기간이 잘렸으면 그 기간을 볼 수 있는 대체 지표를 알린다
    cut_off = p_start is not None and p_start < full["month"].min()
    return ToolResult(
        "ok",
        rows=rows,
        summary=summary,
        chart=chart,
        evidence=_evidence(m, used, len(rows), f"{'연' if grain == 'year' else '월'}별 {_total_word(m) if grain == 'year' else '값'}", **_scope(m, clean)),
        limits=limits,
        alternatives=list(m.alternatives) if cut_off else [],
    )


def _trend_rows(m: Metric, df: pd.DataFrame, grain: str, group_by: str | None) -> list[dict]:
    has_spots = m.id == "spot_visitors"
    frame = df.assign(ok=month_is_complete(m, df))
    keys = ["month"] + ([group_by] if group_by and group_by != "spot" else [])
    if group_by == "spot":
        keys = ["month", "spot"]
    agg = {"value": ("value", "sum"), "ok": ("ok", "max" if has_spots else "min")}
    monthly = frame.groupby(keys, as_index=False).agg(**agg)
    if has_spots:
        counts = frame[frame["value"] > 0].groupby(keys)["spot"].nunique().rename("spotCount").reset_index()
        monthly = monthly.merge(counts, on=keys, how="left").fillna({"spotCount": 0})
    group_cols = keys[1:]
    rows: list[dict] = []
    if grain == "month":
        lookup = {tuple(r[k] for k in keys): r["value"] for r in monthly.to_dict("records")}
        for r in monthly.to_dict("records"):
            before = (r["month"] - 12, *[r[k] for k in group_cols])
            base = lookup.get(before)
            row = {"period": month_text(r["month"]), **{k: r[k] for k in group_cols}, "value": r["value"], "isComplete": bool(r["ok"]), "yoyPct": pct_change(r["value"], base) if base else None}
            if _uses_points(m):
                row["yoyPoints"] = round(r["value"] - base, 1) if base else None
            if has_spots:
                row["spotCount"] = int(r["spotCount"])
            rows.append(row)
        return rows

    monthly["year"] = monthly["month"].dt.year
    yearly = monthly.groupby(["year"] + group_cols, as_index=False).agg(value=("value", m.agg), months=("ok", "sum"))
    if m.agg == "mean":
        yearly["value"] = yearly["value"].round(1)
    yearly["isComplete"] = yearly["months"] == 12
    spot_sets = {}
    if has_spots:
        live = frame[frame["value"] > 0].assign(year=lambda d: d["month"].dt.year)
        spot_sets = {k if isinstance(k, tuple) else (k,): frozenset(v) for k, v in live.groupby(["year"] + group_cols)["spot"]}
    lookup = {tuple(r[k] for k in ["year"] + group_cols): r for r in yearly.to_dict("records")}
    for r in yearly.to_dict("records"):
        key = tuple(r[k] for k in ["year"] + group_cols)
        prior = lookup.get((r["year"] - 1, *key[1:]))
        same_spots = not has_spots or spot_sets.get(key) == spot_sets.get((key[0] - 1, *key[1:]))
        comparable = prior is not None and prior["isComplete"] and r["isComplete"] and same_spots
        row = {"period": str(r["year"]), **{k: r[k] for k in group_cols}, "value": r["value"], "months": int(r["months"]), "isComplete": bool(r["isComplete"]), "yoyPct": pct_change(r["value"], prior["value"]) if comparable else None}
        if _uses_points(m):
            row["yoyPoints"] = round(r["value"] - prior["value"], 1) if comparable else None
        if has_spots:
            row["spotCount"] = len(spot_sets.get(key, ()))
        rows.append(row)
    return rows


def _default_year(m: Metric, df: pd.DataFrame) -> tuple[pd.Period, pd.Period] | None:
    year, _ = _nearest_complete_year(m, df, requested_after=True)
    if year is None:
        return None
    return pd.Period(year=year, month=1, freq="M"), pd.Period(year=year, month=12, freq="M")


def _unit_complete(m: Metric, df: pd.DataFrame, unit: str, month_count: int) -> tuple[pd.Series, pd.Series]:
    """대상별로 기간의 모든 달이 온전한지. (대상별 온전한 달 수, 온전 여부)를 돌려준다."""
    months = df.assign(ok=month_is_complete(m, df)).groupby(unit)["ok"].sum()
    return months, months == month_count


def rank_values(metric: str, start: str | None = None, end: str | None = None, filters: dict | None = None, group_by: str | None = None, top_n: int = 10, ascending: bool = False) -> ToolResult:
    """F2. 순위. 기간의 달을 다 채우지 못한 대상은 순위에 올리지 않고 따로 알린다."""
    m = _metric_or_error(metric)
    if isinstance(m, ToolResult):
        return m
    # 지수는 지역마다 따로 보는 값이라 순위를 낼 때 기본 지역으로 좁히지 않는다
    clean, problem = normalize_filters(m, filters, group_by or (m.default_group if m.agg == "mean" else None))
    if problem:
        return problem
    group = group_by or m.default_group
    if m.id == "visitors_city" and "sigungu" not in clean:
        group = "sigungu"
    if group not in m.dims:
        return invalid(f"{m.id} 은 {', '.join(m.dims)} 로만 묶을 수 있습니다.")
    p_start, p_end = _period_args(start, end)

    # 관광지 지표는 시군으로 묶더라도 관광지별로 온전한지 먼저 본다
    unit = "spot" if m.id == "spot_visitors" else group
    cols = tuple(dict.fromkeys([group, unit]))
    full = fetch(m, clean, cols)
    if full.empty:
        return ToolResult("not_found", "조건에 맞는 자료가 없습니다.", evidence=_evidence(m, "", 0, "조회", **_scope(m, clean)))
    if p_start is None and p_end is None:
        default = _default_year(m, full.groupby("month", as_index=False).agg(value=("value", "sum"), days=("days", "max")))
        if default is None:
            p_start, p_end = full["month"].min(), full["month"].max()
        else:
            p_start, p_end = default
    p_start = p_start if p_start is not None else full["month"].min()
    p_end = p_end if p_end is not None else full["month"].max()
    if crosses_break(m, p_start, p_end):
        return _not_comparable(m, _regime_message(m), clean, _coverage_text(full))
    if p_start < full["month"].min() or p_end > full["month"].max():
        return _rank_out_of_range(m, full, clean, p_start, p_end)
    df = filter_period(full, p_start, p_end)

    month_count = len(pd.period_range(p_start, p_end, freq="M"))
    limits = catalog_limits(m)
    excluded: list[dict] = []
    if m.partial_days_ok:
        kept = df
    else:
        months, complete = _unit_complete(m, df, unit, month_count)
        kept = df[df[unit].isin(complete[complete].index)]
        excluded = [{"name": name, "completeMonths": int(months[name]), "requiredMonths": month_count} for name in complete[~complete].index]
    grouped = kept.groupby(group, as_index=False).agg(value=("value", m.agg))
    if m.agg == "mean":
        grouped["value"] = grouped["value"].round(1)
    grouped = grouped[grouped["value"] > 0].sort_values("value", ascending=ascending)
    label_map = _bus_labels(clean, p_start, p_end) if group == "name" and m.id == "bus_station" else {}
    rows = []
    for i, r in enumerate(grouped.head(max(1, top_n)).to_dict("records"), start=1):
        row = {"rank": i, group: label_map.get(r[group], r[group]), "value": r["value"]}
        if m.id == "spot_visitors" and group != "spot":
            row["spotCount"] = int(kept[(kept[group] == r[group]) & (kept["value"] > 0)]["spot"].nunique())
        rows.append(row)

    summary: dict = {"comparableCount": len(grouped), "excludedCount": len(excluded), "excluded": excluded}
    if m.id == "bus_station":
        summary["excludedDays"] = bus_partial_days(p_start, p_end)
        summary["daysUsed"] = int(df.groupby("month")["days"].max().sum())
        summary["groupedBy"] = "이름이 같은 정류소를 묶음. 방향별 ID 기준 순위는 summary.byStationId"
        limits.insert(0, f"누락으로 보이는 {len(summary['excludedDays'])}일({', '.join(d[5:] for d in summary['excludedDays'])})을 뺀 {summary['daysUsed']}일 기준이다.")
        if group == "name":
            summary["byStationId"] = _bus_id_rank(clean, p_start, p_end)
            top = summary["byStationId"][0]
            limits.insert(1, f"이름이 같은 정류소를 묶은 순위다. 방향별 ID로 나누면 1위는 {top['stationName']} {int(top['value']):,}명이다.")
    chart = {"type": "bar", "x": group, "y": "value", "series": None, "title": f"{m.label} 순위"}
    period = month_range_text(p_start, p_end)
    return ToolResult(
        "ok",
        rows=rows,
        summary=summary,
        chart=chart,
        evidence=_evidence(m, period, len(rows), f"{period} {_total_word(m, '로')} 순위 계산. 기간의 달이 모두 있는 대상만 포함", **_scope(m, clean)),
        limits=limits,
    )


def _rank_out_of_range(m: Metric, full: pd.DataFrame, clean: dict, p_start: pd.Period, p_end: pd.Period) -> ToolResult:
    whole = full.groupby("month", as_index=False).agg(value=("value", "sum"), days=("days", "max"))
    result = _out_of_range(m, whole, clean, p_start, p_end)
    # 묶음을 합친 값은 순위 답이 아니므로 돌려주지 않는다
    result.rows = []
    result.message = f"요청 기간 {month_range_text(p_start, p_end)} 은 제공 기간 {_coverage_text(full)} 을 벗어납니다. 값을 지어내지 말고 summary.nearestYear 같은 제공 기간 안의 해로 다시 조회하세요."
    return result


def _bus_labels(clean: dict, start: pd.Period, end: pd.Period) -> dict:
    params = [clean.get("moveType", "승차"), start.start_time.date(), end.end_time.date()]
    rows = query("SELECT nameKey, mode(stationName) AS label FROM bus_station_daily WHERE moveType = ? AND baseDate BETWEEN ? AND ? AND NOT isPartialDay GROUP BY 1", params)
    return dict(zip(rows["nameKey"], rows["label"]))


def _bus_id_rank(clean: dict, start: pd.Period, end: pd.Period) -> list[dict]:
    params = [clean.get("moveType", "승차"), start.start_time.date(), end.end_time.date()]
    rows = query(
        "SELECT stationId, any_value(stationName) AS stationName, sum(userCnt)::BIGINT AS value FROM bus_station_daily "
        "WHERE moveType = ? AND baseDate BETWEEN ? AND ? AND NOT isPartialDay GROUP BY 1 ORDER BY 3 DESC LIMIT 3",
        params,
    )
    return rows.to_dict("records")


def compare_periods(metric: str, period_a_start: str, period_a_end: str, period_b_start: str, period_b_end: str, filters: dict | None = None, group_by: str | None = None) -> ToolResult:
    """F3. 두 기간 비교. A가 기준이고 B가 비교 대상이다. 두 기간의 달이 모두 온전한 대상만 계산한다."""
    m = _metric_or_error(metric)
    if isinstance(m, ToolResult):
        return m
    clean, problem = normalize_filters(m, filters, group_by)
    if problem:
        return problem
    if group_by and group_by not in m.dims:
        return invalid(f"{m.id} 은 {', '.join(m.dims)} 로만 묶을 수 있습니다.")
    a_start, a_end = parse_month(period_a_start), parse_month(period_a_end, is_end=True)
    b_start, b_end = parse_month(period_b_start), parse_month(period_b_end, is_end=True)
    months_a = len(pd.period_range(a_start, a_end, freq="M"))
    months_b = len(pd.period_range(b_start, b_end, freq="M"))
    if months_a != months_b:
        return _not_comparable(m, f"두 기간의 길이가 다릅니다 ({months_a}개월, {months_b}개월).", clean)

    group = group_by or ("spot" if m.id == "spot_visitors" and "spot" not in clean else None)
    if m.id == "visitors_city" and "sigungu" not in clean:
        group = "sigungu"
    unit = "spot" if m.id == "spot_visitors" else group
    cols = tuple(dict.fromkeys(c for c in [group, unit] if c))
    full = fetch(m, clean, cols)
    if full.empty:
        return ToolResult("not_found", "조건에 맞는 자료가 없습니다.", evidence=_evidence(m, "", 0, "조회", **_scope(m, clean)))
    coverage = _coverage_text(full)
    period_text = f"기준 {month_range_text(a_start, a_end)}, 비교 {month_range_text(b_start, b_end)}"

    missing = [text for s, e, text in [(a_start, a_end, month_range_text(a_start, a_end)), (b_start, b_end, month_range_text(b_start, b_end))] if s < full["month"].min() or e > full["month"].max()]
    if missing:
        reason = f"{m.label} 자료는 {coverage} 만 있어 {', '.join(missing)} 과 비교할 수 없습니다."
        if m.id == "bus_station":
            reason += " 2025-10까지 있는 다른 버스 자료는 인원 정의가 달라 이어 붙이면 안 됩니다."
        return _not_comparable(m, reason, clean, coverage)
    if crosses_break(m, min(a_start, b_start), max(a_end, b_end)):
        return _not_comparable(m, _regime_message(m), clean, coverage)

    frame_a = filter_period(full, a_start, a_end)
    frame_b = filter_period(full, b_start, b_end)
    excluded: list[dict] = []
    if unit:
        ok_a, complete_a = _unit_complete(m, frame_a, unit, months_a)
        ok_b, complete_b = _unit_complete(m, frame_b, unit, months_b)
        names = sorted(set(complete_a.index) | set(complete_b.index))
        comparable = [n for n in names if complete_a.get(n, False) and complete_b.get(n, False)]
        for n in names:
            if n not in comparable:
                excluded.append({"name": n, "completeMonthsA": int(ok_a.get(n, 0)), "completeMonthsB": int(ok_b.get(n, 0)), "requiredMonths": months_a})
        frame_a = frame_a[frame_a[unit].isin(comparable)]
        frame_b = frame_b[frame_b[unit].isin(comparable)]

    limits = catalog_limits(m)
    rows: list[dict] = []
    if group:
        a = frame_a.groupby(group)["value"].agg(m.agg)
        b = frame_b.groupby(group)["value"].agg(m.agg)
        if m.agg == "mean":
            a, b = a.round(1), b.round(1)
        median_base = a.median() if len(a) >= SMALL_BASE_MIN_GROUPS else 0
        for name in a.index:
            rows.append({group: name, "valueA": a[name], "valueB": b.get(name, 0), "ratioPct": ratio_pct(b.get(name, 0), a[name]), "changePct": pct_change(b.get(name, 0), a[name]), "reachedBase": bool(b.get(name, 0) >= a[name]), "isSmallBase": bool(a[name] < median_base * SMALL_BASE_RATIO), **_points(m, b.get(name), a[name])})
        rows.sort(key=lambda r: -(r["ratioPct"] or 0))
    total_a, total_b = _total(m, frame_a["value"]), _total(m, frame_b["value"])
    if not group and (not (month_is_complete(m, frame_a).all() and month_is_complete(m, frame_b).all()) or len(frame_a) != months_a or len(frame_b) != months_b):
        return _not_comparable(m, f"두 기간 중 일부 달의 자료가 온전하지 않아 비교할 수 없습니다. 제공 기간은 {coverage} 입니다.", clean, coverage)
    summary: dict = {"valueA": total_a, "valueB": total_b, "ratioPct": ratio_pct(total_b, total_a), "changePct": pct_change(total_b, total_a), **_points(m, total_b, total_a), "availablePeriod": coverage}
    if group:
        recovered = [r[group] for r in rows if r["reachedBase"]]
        summary.update({"comparableCount": len(rows), "reachedBaseCount": len(recovered), "reachedBase": recovered, "excludedCount": len(excluded), "excluded": excluded})
        limits.append("두 기간의 달이 모두 온전한 대상만 비교했다. 값이 0인 달이 있는 곳은 휴장·미집계일 수 있어 뺐다.")
        if any(r["isSmallBase"] for r in rows):
            limits.append("isSmallBase 인 곳은 기준 값이 작아 비율이 크게 나온다. 실제 증가인지 집계 방식 변화인지 확인되지 않았다.")
    chart = {"type": "bar", "x": group, "y": "ratioPct", "series": None, "title": f"{m.label} 기준 기간 대비(%)"} if group else None
    if group and _uses_points(m):
        chart = {"type": "bar", "x": group, "y": "changePoints", "series": None, "title": f"{m.label} 기준 기간 대비 변화(포인트)"}
    return ToolResult(
        "ok",
        rows=rows,
        summary=summary,
        chart=chart,
        evidence=_evidence(m, period_text, len(rows), f"기준 기간 {_total_word(m)} 대비 비교 기간 {_total_word(m)}의 비율" + ("과 포인트 차이" if _uses_points(m) else ""), **_scope(m, clean)),
        limits=limits,
    )


def get_seasonality(metric: str, filters: dict | None = None, start_year: int | None = None, end_year: int | None = None, include_covid: bool = False) -> ToolResult:
    """F4. 월별 패턴. 해마다 1위 달과, 기간 내내 자료가 있는 같은 대상만의 평균을 함께 돌려준다."""
    m = _metric_or_error(metric)
    if isinstance(m, ToolResult):
        return m
    clean, problem = normalize_filters(m, filters)
    if problem:
        return problem
    if m.id == "visitors_city" and "sigungu" not in clean:
        return ToolResult("needs_clarification", "두 시를 더하지 않으므로 제주시와 서귀포시 중 하나를 골라야 합니다.", summary={"candidates": ["제주시", "서귀포시"]})
    unit = "spot" if m.id == "spot_visitors" else None
    full = fetch(m, clean, (unit,) if unit else ())
    if full.empty:
        return ToolResult("not_found", "조건에 맞는 자료가 없습니다.", evidence=_evidence(m, "", 0, "조회", **_scope(m, clean)))
    total = full.groupby("month", as_index=False).agg(value=("value", "sum"), days=("days", "max"))
    complete_years = _aggregate_years(m, total, None)
    complete_years = sorted(complete_years.loc[complete_years["isComplete"], "year"])
    if not complete_years:
        return ToolResult("insufficient_sample", "12개월이 온전한 해가 없어 계절 패턴을 계산할 수 없습니다.", evidence=_evidence(m, _coverage_text(full), 0, "조회", **_scope(m, clean)), limits=catalog_limits(m))

    last = min(end_year, complete_years[-1]) if end_year else complete_years[-1]
    first = start_year or last - 2
    break_year = pd.Period(m.break_month, freq="M").year + 1 if m.break_month else None
    years = [y for y in complete_years if first <= y <= last and (break_year is None or y >= break_year)]
    excluded_covid = [y for y in years if y in COVID_YEARS and not include_covid]
    years = [y for y in years if y not in excluded_covid]
    if not years:
        return _out_of_range(m, total, clean, pd.Period(year=first, month=1, freq="M"), pd.Period(year=last, month=12, freq="M"))

    frame = full[full["month"].dt.year.isin(years)].assign(year=lambda d: d["month"].dt.year, calMonth=lambda d: d["month"].dt.month)
    by_month = frame.groupby(["year", "calMonth"], as_index=False)["value"].sum()
    peak_by_year = {str(y): int(g.loc[g["value"].idxmax(), "calMonth"]) for y, g in by_month.groupby("year")}
    latest = max(years)
    latest_rows = by_month[by_month["year"] == latest].set_index("calMonth")["value"]

    panel_size = None
    panel_values = by_month.groupby("calMonth")["value"].mean()
    if unit:
        months_needed = 12 * len(years)
        live = frame[frame["value"] > 0].groupby(unit)["month"].nunique()
        panel = live[live == months_needed].index
        panel_size = len(panel)
        panel_values = frame[frame[unit].isin(panel)].groupby(["year", "calMonth"])["value"].sum().groupby("calMonth").mean()
    spot_counts = frame[(frame["year"] == latest) & (frame["value"] > 0)].groupby("calMonth")[unit].nunique() if unit else None

    rows = []
    for month in range(1, 13):
        row = {"month": month, "latestYearValue": latest_rows.get(month), "averageValue": round(panel_values.get(month, float("nan")))}
        if spot_counts is not None:
            row["latestYearSpotCount"] = int(spot_counts.get(month, 0))
        rows.append(row)

    limits = catalog_limits(m)
    if excluded_covid:
        limits.append(f"{', '.join(map(str, excluded_covid))}년은 코로나 영향으로 패턴이 달라 뺐다.")
    if unit:
        limits.append("averageValue 는 기간 내내 값이 있는 같은 관광지만의 평균이고, latestYearValue 는 그해 자료가 있는 관광지의 합이라 곳 수가 달마다 다르다.")
    if len(set(peak_by_year.values())) > 1:
        limits.append("해마다 1위 달이 달라 한 달로 단정하지 않는다.")
    peak_avg = int(panel_values.idxmax()) if panel_values.notna().any() else None
    sorted_latest = latest_rows.sort_values(ascending=False)
    summary = {
        "years": years,
        "latestYear": latest,
        "latestYearTopMonths": [{"month": int(k), "value": v} for k, v in sorted_latest.head(2).items()],
        "peakMonthByYear": peak_by_year,
        "averagePeakMonth": peak_avg,
        "averageBasisCount": panel_size,
        "excludedCovidYears": excluded_covid,
    }
    return ToolResult(
        "ok",
        rows=rows,
        summary=summary,
        chart={"type": "line", "x": "month", "y": "averageValue", "series": None, "title": f"{m.label} 월별 패턴"},
        evidence=_evidence(m, f"{years[0]}~{years[-1]}", len(rows), f"달력 월별 {'값과' if m.agg == 'mean' else '합계와'} 같은 대상의 연평균", **_scope(m, clean)),
        limits=limits,
    )


WEATHER_LIMITS = [
    "원본 누락·중복으로 보이는 이상일은 뺐다.",
    "날씨 값은 4개 관측소 평균이다.",
    "같은 해·같은 달·같은 평일/휴일 구분의 날과 비교했다. 함께 나타나는 관계이며 날씨가 원인이라고 단정하지 않는다.",
]


def get_day_pattern(metric: str, filters: dict | None = None, start: str | None = None, end: str | None = None) -> ToolResult:
    """요일별 하루 평균과 평일·주말 차이. 요일마다 날 수가 달라 합계가 아니라 평균으로 비교한다."""
    m = _metric_or_error(metric)
    if isinstance(m, ToolResult):
        return m
    if not m.daily:
        return _not_comparable(m, f"{m.label} 은 월별 자료라 요일을 나눌 수 없습니다.", {})
    clean, problem = normalize_filters(m, filters)
    if problem:
        return problem
    if m.id == "visitors_city" and "sigungu" not in clean:
        return ToolResult("needs_clarification", "두 시를 더하지 않으므로 제주시와 서귀포시 중 하나를 골라야 합니다.", summary={"candidates": ["제주시", "서귀포시"]})
    days = fetch_daily(m, clean)
    if days.empty:
        return ToolResult("not_found", "조건에 맞는 자료가 없습니다.", evidence=_evidence(m, "", 0, "조회", **_scope(m, clean)))
    coverage = _coverage_text(days)
    p_start, p_end = _period_args(start, end)
    if p_start is None and p_end is None:
        # 계절이 치우치지 않게 가장 최근의 온전한 12개월을 쓴다. 정류소 자료처럼 짧으면 있는 만큼 쓴다
        counts = days.groupby("month", as_index=False).agg(value=("value", "sum"), days=("day", "nunique"))
        complete = counts.loc[month_is_complete(m, counts), "month"]
        p_end = complete.max() if not complete.empty else days["month"].max()
        p_start = p_end - 11
    frame = filter_period(days, p_start, p_end)
    if frame.empty:
        return ToolResult("out_of_range", f"요청 기간에 자료가 없습니다. 제공 기간은 {coverage} 입니다.", summary={"availablePeriod": coverage}, limits=catalog_limits(m))

    used = _coverage_text(frame)
    workdays = frame[~frame["isOffDay"]]
    weekends = frame[frame["isWeekend"]]
    holidays = frame[frame["isOffDay"] & ~frame["isWeekend"]]
    evidence = _evidence(m, used, len(frame), "요일별 하루 평균. 평일은 공휴일을 뺀 날, 주말은 토요일과 일요일", **_scope(m, clean))
    if min(len(workdays), len(weekends)) < MIN_DAY_TYPE_DAYS:
        return ToolResult("insufficient_sample", f"평일 {len(workdays)}일, 주말 {len(weekends)}일뿐이라 차이를 말하지 않습니다.", evidence=evidence, summary={"availablePeriod": coverage}, limits=catalog_limits(m))

    # 평일에 낀 공휴일은 쉬는 날이라 그 요일의 평균에서 뺀다
    usual = frame[frame["isWeekend"] | ~frame["isOffDay"]]
    by_day = usual.groupby("dayOfWeek")["value"].agg(["mean", "count"])
    rows = [{"dayOfWeek": f"{d}요일", "days": int(by_day.loc[d, "count"]), "averageValue": round(by_day.loc[d, "mean"])} for d in DAYS_OF_WEEK if d in by_day.index]
    workday_mean, weekend_mean = workdays["value"].mean(), weekends["value"].mean()
    summary = {
        "availablePeriod": coverage,
        "workdayAverage": round(workday_mean),
        "workdayDays": len(workdays),
        "weekendAverage": round(weekend_mean),
        "weekendDays": len(weekends),
        "weekendVsWorkdayPct": pct_change(weekend_mean, workday_mean),
        "weekdayHolidayDays": len(holidays),
    }
    limits = catalog_limits(m) + [f"하루 평균을 비교한 값이다. 평일 평균은 평일 공휴일 {len(holidays)}일을 뺀 {len(workdays)}일 기준이고, 주말은 {len(weekends)}일 기준이다."]
    chart = {"type": "bar", "x": "dayOfWeek", "y": "averageValue", "series": None, "title": f"{m.label} 요일별 하루 평균"}
    return ToolResult("ok", rows=rows, summary=summary, chart=chart, evidence=evidence, limits=limits)


def _weather_effect_rows(frame: pd.DataFrame, weather: str, bins: list[float] | None) -> tuple[list[dict], str]:
    cell = ["year", "month", "isOffDay"]
    if weather == "rain":
        edges = sorted(bins or [1, 10, 30])
        column = "rainMm"
        unit = "mm"
        base = frame[frame["rainMm"] < edges[0]].groupby(cell)["boardingCnt"].mean().rename("base").reset_index()
        method = f"같은 해·달·평일/휴일의 비 안 온 날({edges[0]:g}mm 미만) 평균과 일별로 비교한 비율의 평균"
    else:
        edges = sorted(bins or [10, 20, 25])
        column = "avgTa"
        unit = "℃"
        base = frame.groupby(cell)["boardingCnt"].mean().rename("base").reset_index()
        method = "같은 해·달·평일/휴일의 평균과 일별로 비교한 비율의 평균"
    joined = frame.merge(base, on=cell, how="inner")
    groups = []
    if weather == "rain":
        for low, high in zip(edges, edges[1:] + [None]):
            groups.append((f"{low:g}~{high:g}{unit}" if high else f"{low:g}{unit} 이상", low, high))
    else:
        groups.append((f"{edges[0]:g}{unit} 미만", None, edges[0]))
        for low, high in zip(edges, edges[1:] + [None]):
            groups.append((f"{low:g}~{high:g}{unit}" if high else f"{low:g}{unit} 이상", low, high))
    rows = []
    for label, low, high in groups:
        mask = pd.Series(True, index=joined.index)
        if low is not None:
            mask &= joined[column] >= low
        if high is not None:
            mask &= joined[column] < high
        part = joined[mask]
        enough = len(part) >= MIN_WEATHER_DAYS
        change = round(100 * ((part["boardingCnt"] / part["base"]).mean() - 1), 1) if enough else None
        rows.append({"range": label, "days": len(part), "changePct": change, "isSufficient": enough})
    return rows, method


def analyze_weather_effect(weather: str = "rain", bins: list[float] | None = None, start: str | None = None, end: str | None = None, date: str | None = None) -> ToolResult:
    """F5. 날씨에 따른 버스 승차 변화. bus_weather_daily 만 쓴다. date 를 주면 그날을 평소와 비교한다."""
    if weather not in ("rain", "temperature"):
        return invalid("weather 는 rain 또는 temperature 입니다.")
    all_days = query("SELECT * FROM bus_weather_daily ORDER BY baseDate")
    all_days["baseDate"] = pd.to_datetime(all_days["baseDate"])
    p_start, p_end = _period_args(start, end)
    frame = all_days
    if p_start is not None:
        frame = frame[frame["baseDate"] >= p_start.start_time]
    if p_end is not None:
        frame = frame[frame["baseDate"] <= p_end.end_time]
    last_day = all_days["baseDate"].max().strftime("%Y-%m-%d")
    first_day = all_days["baseDate"].min().strftime("%Y-%m-%d")
    coverage = f"{first_day}~{last_day}"
    if frame.empty:
        return ToolResult("out_of_range", f"요청 기간에 자료가 없습니다. 비 영향 분석 자료는 {coverage} 입니다.", summary={"availablePeriod": coverage}, limits=list(WEATHER_LIMITS))

    rows, method = _weather_effect_rows(frame, weather, bins)
    period = f"{frame['baseDate'].min():%Y-%m-%d}~{frame['baseDate'].max():%Y-%m-%d}"
    evidence = {"table": "bus_weather_daily", "catalogId": "bus_route_daily, asos_daily", "period": period, "rowCount": len(frame), "method": method}
    chart = {"type": "bar", "x": "range", "y": "changePct", "series": None, "title": "날씨 구간별 버스 승차 변화(%)"}
    summary: dict = {"availablePeriod": coverage, "excludedAbnormalDays": _abnormal_day_count()}
    if date:
        return _weather_on_date(date, all_days, rows, summary, evidence, chart, coverage)
    if not any(r["isSufficient"] for r in rows):
        return ToolResult("insufficient_sample", f"구간별 날 수가 {MIN_WEATHER_DAYS}일 미만이라 값을 말하지 않습니다.", rows=rows, summary=summary, evidence=evidence, limits=list(WEATHER_LIMITS))
    return ToolResult("ok", rows=rows, summary=summary, chart=chart, evidence=evidence, limits=list(WEATHER_LIMITS))


def _abnormal_day_count() -> int:
    return int(query("SELECT count(*) AS n FROM bus_boarding_daily WHERE isAbnormalDay")["n"][0])


def _weather_on_date(date_text: str, all_days: pd.DataFrame, rows: list[dict], summary: dict, evidence: dict, chart: dict, coverage: str) -> ToolResult:
    try:
        day = Date.fromisoformat(date_text)
    except ValueError:
        return invalid(f"date 는 YYYY-MM-DD 형식으로 주세요: {date_text}")
    weather = query("SELECT rainMm, rainMaxMm, avgTa FROM weather_island_daily WHERE baseDate = ?", [day])
    day_info: dict = {"date": date_text, "rainMm": round(weather["rainMm"][0], 1) if len(weather) else None, "rainMaxMm": round(weather["rainMaxMm"][0], 1) if len(weather) else None}
    summary["dateCheck"] = day_info
    limits = list(WEATHER_LIMITS)

    match = all_days[all_days["baseDate"] == pd.Timestamp(day)]
    if len(match):
        target = match.iloc[0]
        peers = all_days[(all_days["year"] == target["year"]) & (all_days["month"] == target["month"]) & (all_days["isOffDay"] == target["isOffDay"]) & (all_days["rainMm"] < 1)]
        peers = peers[peers["baseDate"] != target["baseDate"]]
        if len(peers) >= 3:
            day_info.update({"boardingCnt": target["boardingCnt"], "usualDryDayAverage": round(peers["boardingCnt"].mean()), "comparedDays": len(peers), "changePct": pct_change(target["boardingCnt"], peers["boardingCnt"].mean())})
            return ToolResult("ok", rows=rows, summary=summary, chart=chart, evidence=evidence, limits=limits)
        return ToolResult("insufficient_sample", f"같은 달 비 안 온 비교일이 {len(peers)}일뿐이라 그날의 변화를 말하지 않습니다. 장기 구간별 값은 rows 에 있습니다.", rows=rows, summary=summary, chart=chart, evidence=evidence, limits=limits)

    partial = query("SELECT bool_or(isPartialDay) AS p, count(*) AS n FROM bus_station_daily WHERE baseDate = ?", [day])
    is_partial = partial["n"][0] > 0 and bool(partial["p"][0])
    abnormal = query("SELECT isAbnormalDay FROM bus_boarding_daily WHERE baseDate = ?", [day])
    if is_partial:
        day_info["isPartialBusDay"] = True
        limits.insert(0, "일부 누락 추정은 건수와 정류소 이름 비어 있음을 근거로 판단한 것이며 제공처에 확인한 것은 아니다.")
        message = f"{date_text} 은 버스 자료가 일부 누락으로 보여 그날의 감소폭을 말할 수 없습니다. 비 영향 분석 자료는 {coverage} 입니다. 장기 구간별 값은 rows 에 있습니다."
    elif len(abnormal) and bool(abnormal["isAbnormalDay"][0]):
        day_info["isAbnormalDay"] = True
        message = f"{date_text} 은 원본 누락·중복으로 보이는 이상일이라 그날의 변화를 말할 수 없습니다. 장기 구간별 값은 rows 에 있습니다."
    else:
        message = f"{date_text} 은 비 영향 분석 자료({coverage}) 밖이고 다른 버스 자료와는 인원 정의가 달라 비교할 수 없습니다. 장기 구간별 값은 rows 에 있습니다."
    return ToolResult("not_comparable", message, rows=rows, summary=summary, chart=chart, evidence=evidence, limits=limits)


# 이보다 길게 열리는 행사(상설 전시 등)는 그 기간의 행사 목록에서 따로 센다
LONG_EVENT_DAYS = 90
MAX_EVENT_ROWS = 30


def list_events(start: str | None = None, end: str | None = None, keyword: str | None = None) -> ToolResult:
    """행사 찾기. 기간과 겹치거나 이름에 낱말이 들어간 행사를 시작일 순으로 돌려준다. 행사 수 비교에는 쓰지 않는다."""
    words = _tokens(keyword or "")
    if not (start or end or words):
        return invalid("start, end 또는 keyword 중 하나는 있어야 합니다.")
    events = query("SELECT eventName, startDate, endDate, durationDays, startTime, endTime FROM jeju_events ORDER BY startDate, eventName")
    events["startDate"] = pd.to_datetime(events["startDate"])
    events["endDate"] = pd.to_datetime(events["endDate"])
    first, last = events["startDate"].min(), events["endDate"].max()
    coverage = f"{first:%Y-%m-%d}~{last:%Y-%m-%d}"
    per_year = events.groupby(events["startDate"].dt.year).size()
    busy, quiet = per_year.idxmax(), per_year[per_year.index >= 2017].idxmin()
    limits = [
        "장소 항목이 없어 어디서 열렸는지 알 수 없다.",
        f"해마다 등록 건수가 크게 달라({quiet}년 {per_year[quiet]}건, {busy}년 {per_year[busy]}건) 행사 수가 늘거나 줄었다고 말하지 않는다. 목록에 없는 행사도 있을 수 있다.",
        "행사와 방문객 변화가 함께 나타나도 행사 때문이라고 단정하지 않는다.",
    ]
    evidence = {"table": "jeju_events", "catalogId": "jeju_events", "label": "제주 주요 행사", "method": "기간과 하루라도 겹치거나 이름에 낱말이 모두 들어간 행사"}

    p_start, p_end = _period_args(start, end)
    frame = events
    if p_start is not None or p_end is not None:
        lo = (p_start or p_end).start_time
        hi = (p_end or p_start).end_time
        if hi < first or lo > last:
            return ToolResult("out_of_range", f"요청 기간에 행사 자료가 없습니다. 제공 기간은 {coverage} 입니다.", summary={"availablePeriod": coverage}, evidence={**evidence, "period": coverage, "rowCount": 0}, limits=limits)
        frame = frame[(frame["startDate"] <= hi) & (frame["endDate"] >= lo)]
        evidence["period"] = month_range_text(p_start or p_end, p_end or p_start)
    if words:
        names = frame["eventName"].str.replace(" ", "")
        frame = frame[names.map(lambda n: all(w.replace(" ", "") in n for w in words))]
    if frame.empty:
        return ToolResult("not_found", f"조건에 맞는 행사가 없습니다. 제공 기간은 {coverage} 입니다.", summary={"availablePeriod": coverage}, evidence={**evidence, "rowCount": 0}, limits=limits)

    long_running = frame[frame["durationDays"] > LONG_EVENT_DAYS]
    shown = pd.concat([frame[frame["durationDays"] <= LONG_EVENT_DAYS], long_running]).head(MAX_EVENT_ROWS)
    rows = [
        {"eventName": r["eventName"], "startDate": f"{r['startDate']:%Y-%m-%d}", "endDate": f"{r['endDate']:%Y-%m-%d}", "durationDays": int(r["durationDays"]), "isLongRunning": bool(r["durationDays"] > LONG_EVENT_DAYS)}
        for r in shown.to_dict("records")
    ]
    summary = {"availablePeriod": coverage, "matchCount": len(frame), "shownCount": len(rows), "longRunningCount": len(long_running)}
    if len(frame) > len(rows):
        limits.insert(0, f"조건에 맞는 {len(frame)}건 중 {len(rows)}건만 보여 준다.")
    if len(long_running):
        limits.append(f"{LONG_EVENT_DAYS}일 넘게 열리는 행사 {len(long_running)}건(상설 전시 등)은 목록 뒤에 두었다.")
    # 표에 일부만 보여 줘도 건수는 matchCount 다. 작은 모델이 보여 준 행 수로 어림하지 않게 문장으로 알린다
    message = f"조건에 맞는 행사는 {len(frame)}건입니다." + (f" rows 에는 {len(rows)}건만 있습니다." if len(frame) > len(rows) else "")
    return ToolResult("ok", message, rows=rows, summary=summary, evidence={**evidence, "rowCount": len(rows)}, limits=limits)
