"""제주빅데이터센터에서 내려받은 CSV를 넣어 분석한다.

센터 원본은 반출할 수 없어 디스크에 저장하지 않고 화면 세션의 메모리에만 둔다 (퇴실하면 지워진다).
LLM과 화면에는 10 미만 칸을 상위 단위로 묶거나 가린 집계만 내보낸다. 원본 행은 내보내지 않는다.
지원하는 데이터는 카드 이용 현황(읍면동)_v02(1-08)와 단기체류 외국인 유동인구(읍면동)_v02(4-15)다.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

from agent.common import ToolResult, invalid, month_range_text, parse_month, query

# 이 값보다 작은 칸은 그대로 내보내지 않는다 (제주데이터허브 반출 기준)
THRESHOLD = 10
# 작은 칸이 이 비율보다 많으면 칸을 가리는 대신 표 전체를 상위 단위로 다시 묶는다
ROLLUP_SHARE = 0.2
FOREIGN_TYPES = ("중국", "일본", "동남아", "기타외국")
VIRTUAL_MARK = "가상"
# 4-15 국적코드 (명세 v13 [참고1] 31개). 화면과 답변에는 한글 이름을 쓴다
NATIONALITY_NAMES = {"AUS": "오스트레일리아", "BGD": "방글라데시", "CAN": "캐나다", "CHN": "중국", "DEU": "독일", "EGY": "이집트", "ETC": "기타", "FRA": "프랑스", "GBR": "영국", "IDN": "인도네시아", "IND": "인도", "JPN": "일본", "KAZ": "카자흐스탄", "KGZ": "키르기스스탄", "KHM": "캄보디아", "LKA": "스리랑카", "MGL": "몽골", "MMR": "미얀마", "MYS": "말레이시아", "NGR": "나이지리아", "NPL": "네팔", "NZL": "뉴질랜드", "PAK": "파키스탄", "PHL": "필리핀", "RUS": "러시아", "THA": "태국", "TWN": "타이완", "UKR": "우크라이나", "USA": "미국", "UZB": "우즈베키스탄", "VNM": "베트남"}


@dataclass(frozen=True)
class Dim:
    key: str  # LLM이 고르는 이름
    label: str
    # 다음 상위 단위. None 이면 없애서(전체로) 묶는다. 묶지 않는 차원은 rollable=False
    parent: str | None = None
    rollable: bool = True
    # 작은 칸만 이 이름으로 합친다 (국적, 업종처럼 순위를 보려는 묶음). 표 전체를 묶는 것보다 먼저 한다
    merge_label: str | None = None


@dataclass(frozen=True)
class CenterSpec:
    center_no: str
    title: str
    measures: dict[str, tuple[str, str]]  # 이름: (설명, 단위)
    dims: dict[str, Dim]
    # 10 미만이면 상위 단위로 묶을 차원의 순서 (기간 → 국적·이용자 → 지역 → 그 밖)
    rollup_order: tuple[str, ...]
    download: str
    limits: tuple[str, ...]
    # 무엇이 10명 미만이면 가리는지. 사용자에게 가림을 설명할 때 쓴다
    basis: str = ""

    @property
    def name(self) -> str:
        """화면과 답변에 쓰는 이름. 버전 표기(_v02)는 센터 조회 화면에서 찾을 때만 필요하다."""
        return self.title.removesuffix("_v02")


SPECS: dict[str, CenterSpec] = {
    "1-08": CenterSpec(
        "1-08",
        "카드 이용 현황(읍면동)_v02",
        {"use_amt": ("이용금액", "원"), "use_cnt": ("이용건수", "건"), "user_cnt": ("이용자수", "명")},
        {
            "month": Dim("month", "월", "quarter"),
            "quarter": Dim("quarter", "분기", "year"),
            "year": Dim("year", "연"),
            "region": Dim("region", "권역", "city"),
            "city": Dim("city", "시"),
            "areaType": Dim("areaType", "도심·읍면"),
            "dong": Dim("dong", "법정동", "region"),
            "industry": Dim("industry", "업종", rollable=False, merge_label="그 밖의 업종"),
            "userType": Dim("userType", "이용자", "userGroup"),
            "userGroup": Dim("userGroup", "이용자 구분(외국인 합계)"),
            "tourType": Dim("tourType", "관광구분"),
            "age": Dim("age", "연령대"),
            "sex": Dim("sex", "성별"),
        },
        ("month", "quarter", "userType", "dong", "region", "age", "sex", "tourType", "areaType"),
        "센터 PC의 데이터 조회 화면에서 '카드 이용 현황(읍면동)_v02' 를 검색하고, 검색 기간을 한 달로 정해 조회한 뒤 CSV로 내려받습니다. "
        "한 번에 한 달씩 받을 수 있어 여러 달은 달마다 반복하거나 센터 담당자에게 미리 받아 달라고 요청합니다. 받은 CSV 파일을 이 화면 왼쪽 'CSV 넣기' 에 올립니다.",
        (
            "비씨카드로 결제한 금액만 들어 있어 제주 전체 소비액이 아니다.",
            "이용자수는 달·칸마다 따로 센 값이라 여러 달이나 칸을 더하면 연인원이 된다.",
            "같은 이름의 법정동(월평동)이 제주시와 서귀포시에 하나씩 있어 시 이름을 붙여 구분했다.",
        ),
        "이용자수가 10명 미만인 칸은 이용금액과 이용건수도 함께 가렸다.",
    ),
    "4-15": CenterSpec(
        "4-15",
        "단기체류 외국인 유동인구(읍면동)_v02",
        {"visit_pop": ("시간당 평균 방문인구", "명")},
        {
            "month": Dim("month", "월", "quarter"),
            "quarter": Dim("quarter", "분기", "year"),
            "year": Dim("year", "연"),
            "hour": Dim("hour", "시간"),
            "region": Dim("region", "권역", "city"),
            "city": Dim("city", "시"),
            "dong": Dim("dong", "행정동", "region"),
            "nationality": Dim("nationality", "국적", merge_label="그 밖의 국적"),
        },
        ("month", "quarter", "nationality", "dong", "region", "hour"),
        "센터 PC의 데이터 조회 화면에서 '단기체류 외국인 유동인구(읍면동)_v02' 를 검색하고, 검색 기간을 한 달로 정해 조회한 뒤 CSV로 내려받습니다. "
        "읍면동을 지정하지 않으면 전체 읍면동이 함께 받아집니다. 한 번에 한 달씩이라 여러 달은 달마다 반복하거나 센터 담당자에게 미리 받아 달라고 요청합니다. 받은 CSV 파일을 이 화면 왼쪽 'CSV 넣기' 에 올립니다.",
        (
            "SKT 통신 자료로 만든 추정치다.",
            "각 시각 정각에 잰 인원의 평균이다. 같은 사람이 여러 시각에 세어지므로 하루 방문객 수가 아니며, 시각끼리 더하지 않는다.",
            "자료에 행이 없는 시각은 0명으로 보고 평균했다.",
        ),
        "시간당 평균 인원이 10명 미만인 칸을 가렸다.",
    ),
}
COMMON_LIMITS = (
    "센터 반출 불가 원본을 이 화면 안에서만 집계했다. 원본 행은 저장하지 않고 내보내지 않는다.",
    f"값 가림이란: 센터 반출 기준에 따라 사람 수가 {THRESHOLD}명 미만인 칸은 누구인지 짐작될 수 있어 값을 보여 주지 않는다. 그런 칸은 더 큰 단위(월→분기, 법정동→권역 등)로 묶거나 '가림'으로 표시하고, 가린 값은 추정하지 않는다.",
    "같은 자료를 다른 묶음으로 여러 번 조회하면 가린 값이 역산될 수 있어, 결과를 반출하려면 센터의 반출 신청과 검수를 거친다.",
)


@dataclass
class LoadedFile:
    name: str
    center_no: str
    months: list[str]
    rows: int
    virtual: bool


@dataclass
class CenterStore:
    """한 화면 세션에서 넣은 센터 CSV. 메모리에만 있고 세션이 끝나면 사라진다."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    files: list[LoadedFile] = field(default_factory=list)

    def is_virtual(self, center_no: str) -> bool:
        return any(f.virtual for f in self.files if f.center_no == center_no)

    def months(self, center_no: str) -> list[str]:
        frame = self.frames.get(center_no)
        return sorted(frame["month"].unique()) if frame is not None else []

    def summary(self) -> list[dict]:
        out = []
        for no, frame in self.frames.items():
            months = self.months(no)
            out.append({"centerNo": no, "title": SPECS[no].title, "period": f"{months[0]}~{months[-1]}" if months else "", "months": len(months), "virtual": self.is_virtual(no)})
        return out

    def clear(self) -> None:
        self.frames.clear()
        self.files.clear()

    def load(self, name: str, data: bytes) -> LoadedFile:
        """CSV 하나를 확인해 넣는다. 문제가 있으면 사용자에게 보여 줄 문장으로 ValueError 를 낸다."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(f"{name}: UTF-8 CSV가 아닙니다. 센터에서 내려받은 CSV를 그대로 넣어 주세요.") from None
        raw = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, na_values=["NA"])
        raw.columns = [c.strip().strip('"') for c in raw.columns]
        center_no = _detect(name, list(raw.columns))
        if raw.empty:
            raise ValueError(f"{name}: 행이 없습니다.")
        frame = _NORMALIZERS[center_no](raw, name)
        new_months = sorted(frame["month"].unique())
        overlap = sorted(set(new_months) & set(self.months(center_no)))
        if overlap:
            raise ValueError(f"{name}: {', '.join(overlap)} 자료를 이미 넣었습니다. 같은 달을 두 번 넣으면 값이 두 배가 됩니다.")
        old = self.frames.get(center_no)
        self.frames[center_no] = frame if old is None else pd.concat([old, frame], ignore_index=True)
        loaded = LoadedFile(name, center_no, new_months, len(frame), VIRTUAL_MARK in name)
        self.files.append(loaded)
        return loaded


def _spec_columns() -> dict[str, list[str]]:
    rows = query("SELECT centerNo, columnName FROM center_columns ORDER BY centerNo, ordinal")
    return {no: list(group["columnName"]) for no, group in rows.groupby("centerNo")}


def _detect(name: str, columns: list[str]) -> str:
    """헤더로 어느 센터 데이터인지 정한다. 명세 v13의 영문 컬럼과 같아야 한다."""
    specs = _spec_columns()
    matched = [no for no, cols in specs.items() if set(cols) == set(columns)]
    supported = [no for no in matched if no in SPECS]
    if supported:
        return supported[0]
    if matched:
        titles = query("SELECT centerNo, title FROM center_datasets WHERE centerNo IN (SELECT unnest(?))", [matched])
        title = ", ".join(f"{r.centerNo} {r.title}" for r in titles.itertuples())
        raise ValueError(f"{name}: {title} 자료로 보입니다. 이 자료는 아직 분석 기능이 없습니다. 지금은 {', '.join(f'{s.center_no} {s.title}' for s in SPECS.values())} 만 넣을 수 있습니다.")
    best = max(SPECS, key=lambda no: len(set(specs[no]) & set(columns)))
    missing = [c for c in specs[best] if c not in columns]
    extra = [c for c in columns if c not in specs[best]]
    raise ValueError(
        f"{name}: 센터 명세와 열이 맞지 않습니다. 가장 가까운 자료는 {best} {SPECS[best].title} 입니다. "
        f"없는 열: {', '.join(missing) or '없음'} / 명세에 없는 열: {', '.join(extra) or '없음'}"
    )


def _numbers(raw: pd.DataFrame, columns: list[str], name: str) -> dict[str, pd.Series]:
    out = {}
    for col in columns:
        values = pd.to_numeric(raw[col], errors="coerce")
        bad = values.isna() & raw[col].notna()
        if bad.any():
            raise ValueError(f"{name}: {col} 에 숫자가 아닌 값이 {int(bad.sum())}개 있습니다 (예: {raw.loc[bad, col].iloc[0]}).")
        out[col] = values.fillna(0)
    return out


def _quarter(month: pd.Series) -> pd.Series:
    period = pd.PeriodIndex(month, freq="M")
    return pd.Series([f"{p.year}년 {p.quarter}분기" for p in period], index=month.index)


def _normalize_card(raw: pd.DataFrame, name: str) -> pd.DataFrame:
    nums = _numbers(raw, ["user_cnt", "use_cnt", "use_amt"], name)
    month = pd.to_datetime(raw["base_year_month"].str.strip(), format="%Y%m", errors="coerce")
    if month.isna().any():
        raise ValueError(f"{name}: base_year_month 가 YYYYMM 이 아닌 행이 {int(month.isna().sum())}개 있습니다.")
    frame = pd.DataFrame(
        {
            "month": month.dt.strftime("%Y-%m"),
            "region": raw["dstrct_se"],
            "city": raw["city"],
            "areaType": raw["area_type"],
            # 월평동이 두 시에 하나씩 있어 시 이름을 붙인다
            "dong": raw["city"].fillna("") + " " + raw["bjd"].fillna(""),
            "industry": raw["biz_nm"],
            "userType": raw["user_type"],
            "tourType": raw["tour_type"].fillna("NA"),
            "age": raw["age"].fillna("NA").map(lambda a: a if a == "NA" else f"{a}대"),
            "sex": raw["sex"],
            "user_cnt": nums["user_cnt"],
            "use_cnt": nums["use_cnt"],
            "use_amt": nums["use_amt"],
        }
    )
    frame["userGroup"] = frame["userType"].where(~frame["userType"].isin(FOREIGN_TYPES), "외국인")
    return _add_time(frame)


def _normalize_foreign(raw: pd.DataFrame, name: str) -> pd.DataFrame:
    nums = _numbers(raw, ["visit_pop_cnt"], name)
    day = pd.to_datetime(raw["base_date"].str.strip(), format="%Y%m%d", errors="coerce")
    if day.isna().any():
        raise ValueError(f"{name}: base_date 가 YYYYMMDD 가 아닌 행이 {int(day.isna().sum())}개 있습니다.")
    hour = pd.to_numeric(raw["time"], errors="coerce")
    if hour.isna().any() or not hour.between(1, 24).all():
        raise ValueError(f"{name}: time 이 1~24 가 아닌 행이 있습니다.")
    emd = raw["emd_nm"].fillna("")
    frame = pd.DataFrame(
        {
            "day": day.dt.strftime("%Y-%m-%d"),
            "month": day.dt.strftime("%Y-%m"),
            "hour": hour.astype(int).map(lambda h: f"{h:02d}시"),
            "region": raw["dstrct_se"],
            "city": emd.str.split(" ").str[0],
            "dong": emd,
            "nationality": raw["nationality"].map(lambda c: NATIONALITY_NAMES.get(c, c)),
            "visit_pop": nums["visit_pop_cnt"],
        }
    )
    return _add_time(frame)


def _add_time(frame: pd.DataFrame) -> pd.DataFrame:
    frame["quarter"] = _quarter(frame["month"])
    frame["year"] = frame["month"].str[:4] + "년"
    return frame


_NORMALIZERS = {"1-08": _normalize_card, "4-15": _normalize_foreign}


def _aggregate(spec: CenterSpec, frame: pd.DataFrame, dims: list[str], measure: str) -> pd.DataFrame:
    """묶음별 값. 카드는 합계, 유동인구는 그 기간 모든 시각에 대한 평균(행이 없는 시각은 0)이다.

    count 는 10 미만 판단에 쓰는 값이다. 카드는 이용자수, 유동인구는 평균 인원 자체다.
    """
    if spec.center_no == "1-08":
        cols = ["user_cnt", measure] if measure != "user_cnt" else ["user_cnt"]
        out = frame.groupby(dims, dropna=False, as_index=False)[cols].sum() if dims else frame[cols].sum().to_frame().T
        out["value"] = out[measure]
        out["count"] = out["user_cnt"]
        return out[dims + ["value", "count"]]
    # 시각 수: 묶음에 들어 있는 기간·시간 조건마다 자료가 있는 날짜×시각 수
    time_dims = [d for d in dims if d in ("month", "quarter", "year", "hour")]
    slots = frame.drop_duplicates(["day", "hour"])
    slots = slots.groupby(time_dims, as_index=False).size().rename(columns={"size": "slots"}) if time_dims else pd.DataFrame({"slots": [len(slots)]})
    total = frame.groupby(dims, dropna=False, as_index=False)["visit_pop"].sum() if dims else frame[["visit_pop"]].sum().to_frame().T
    out = total.merge(slots, on=time_dims, how="left") if time_dims else total.assign(slots=int(slots["slots"].iloc[0]))
    out["value"] = (out["visit_pop"] / out["slots"]).round(1)
    out["count"] = out["value"]
    return out[dims + ["value", "count"]]


def _secondary(table: pd.DataFrame, dims: list[str]) -> int:
    """가린 칸이 한 줄에 하나뿐이면 합계에서 빼서 알아낼 수 있으므로 그 줄의 가장 작은 칸을 하나 더 가린다."""
    added = 0
    changed = True
    while changed:
        changed = False
        lines = [[d for d in dims if d != dim] for dim in dims] or [[]]
        for others in lines:
            groups = table.groupby(others, dropna=False).groups.values() if others else [table.index]
            for index in groups:
                part = table.loc[index]
                if len(part) > 1 and part["suppressed"].sum() == 1:
                    target = part[~part["suppressed"]]["count"].idxmin()
                    table.loc[target, "suppressed"] = True
                    added += 1
                    changed = True
    return added


def protect(spec: CenterSpec, frame: pd.DataFrame, dims: list[str], measure: str) -> tuple[pd.DataFrame, list[str], dict]:
    """10 미만 칸을 처리한 표, 바뀐 묶음 기준, 처리 내역을 돌려준다.

    작은 칸이 많으면 기간 → 국적·이용자 → 지역 순으로 표 전체를 상위 단위로 다시 묶고,
    남은 작은 칸은 가린 뒤 합계로 역산되지 않게 한 칸씩 더 가린다.
    """
    dims = list(dims)
    rollups: list[str] = []
    merged: list[str] = []
    table = _aggregate(spec, frame, dims, measure)
    for dim in [d for d in dims if spec.dims[d].merge_label]:
        small = table.loc[table["count"] < THRESHOLD, dims]
        # 합칠 칸이 둘 이상이어야 합친 값으로 원래 칸을 알 수 없다
        if len(small) < 2:
            continue
        label = spec.dims[dim].merge_label
        frame = frame.merge(small.assign(_small=True), on=dims, how="left")
        frame[dim] = frame[dim].where(frame.pop("_small").isna(), label)
        merged.append(f"{spec.dims[dim].label} {len(small)}칸 → {label}")
        table = _aggregate(spec, frame, dims, measure)
    while len(table) and (table["count"] < THRESHOLD).mean() > ROLLUP_SHARE:
        step = next((d for d in spec.rollup_order if d in dims and spec.dims[d].rollable), None)
        if step is None:
            break
        parent = spec.dims[step].parent
        rollups.append(f"{spec.dims[step].label} → {spec.dims[parent].label if parent else '전체'}")
        dims = [parent if d == step else d for d in dims if not (parent is None and d == step)]
        dims = list(dict.fromkeys(dims))
        table = _aggregate(spec, frame, dims, measure)
    table = table.reset_index(drop=True)
    table["suppressed"] = table["count"] < THRESHOLD
    primary = int(table["suppressed"].sum())
    secondary = _secondary(table, dims) if primary else 0
    table.loc[table["suppressed"], "value"] = None
    return table.drop(columns="count"), dims, {"merged": merged, "rollups": rollups, "suppressedCells": primary, "extraSuppressedCells": secondary}


def _filter(spec: CenterSpec, frame: pd.DataFrame, filters: dict) -> tuple[pd.DataFrame, ToolResult | None]:
    # 이름 후보는 조건을 걸기 전 자료에서 찾는다. 다른 조건과 겹치는 행이 없으면 결과가 비어 not_found 가 된다
    base = frame
    for key, value in filters.items():
        if key not in spec.dims or key in ("quarter", "year"):
            return frame, invalid(f"{spec.center_no} 에 쓸 수 없는 조건입니다: {key}. 가능한 조건: {', '.join(k for k in spec.dims if k not in ('quarter', 'year'))}")
        if key == "userType" and value == "외국인":
            frame = frame[frame["userGroup"] == "외국인"]
            continue
        choices = sorted(base[key].dropna().unique())
        if value not in choices:
            near = [c for c in choices if str(value) in str(c) or str(c) in str(value)]
            if len(near) != 1:
                return frame, ToolResult("needs_clarification", f"{spec.dims[key].label} '{value}' 을 찾지 못했습니다. 다음 중에서 골라 주세요.", summary={"candidates": near or choices[:30]})
            value = near[0]
        frame = frame[frame[key] == value]
    return frame, None


def analyze_center_csv(
    store: CenterStore | None,
    dataset: str,
    measure: str | None = None,
    group_by: list[str] | None = None,
    filters: dict | None = None,
    start: str | None = None,
    end: str | None = None,
) -> ToolResult:
    """사용자가 넣은 센터 CSV를 집계한다. 10 미만 칸을 처리한 결과만 돌려준다."""
    spec = SPECS.get(dataset)
    if spec is None:
        return invalid(f"이 도구로 분석할 수 있는 센터 자료는 {', '.join(SPECS)} 뿐입니다.")
    frame = store.frames.get(dataset) if store else None
    if frame is None:
        return ToolResult(
            "center_only",
            f"'{spec.title}'({dataset}) CSV를 아직 넣지 않아 계산할 수 없습니다. 센터에서 내려받아 넣으면 계산할 수 있습니다.",
            summary={"title": spec.title, "download": spec.download, "loaded": store.summary() if store else []},
            limits=["센터 원본은 센터 PC에서 직접 내려받아 넣어야 한다. 이 프로그램이 센터 시스템에 직접 접속하지 않는다."],
        )
    measure = measure or next(iter(spec.measures))
    if measure not in spec.measures:
        return invalid(f"{dataset} 의 값은 {', '.join(spec.measures)} 중 하나입니다.")
    group_by = list(dict.fromkeys(group_by or []))
    bad = [g for g in group_by if g not in spec.dims]
    if bad or len(group_by) > 2:
        return invalid(f"묶음은 2개까지이고 {', '.join(spec.dims)} 중에서 고릅니다.")

    months = sorted(frame["month"].unique())
    first, last = parse_month(months[0]), parse_month(months[-1])
    a = parse_month(start) if start else first
    b = parse_month(end, is_end=True) if end else last
    have = f"{months[0]}~{months[-1]} ({len(months)}개월)"
    if b < first or a > last:
        return ToolResult("out_of_range", f"넣은 자료는 {have} 입니다. 요청한 {month_range_text(a, b)} 자료는 없습니다.", summary={"availablePeriod": have})
    wanted = {str(p) for p in pd.period_range(max(a, first), min(b, last), freq="M")}
    missing = sorted(wanted - set(months))
    period = frame[frame["month"].isin(wanted)]
    period, problem = _filter(spec, period, filters or {})
    if problem:
        return problem
    if period.empty:
        return ToolResult("not_found", "조건에 맞는 행이 없습니다.", summary={"availablePeriod": have})

    table, dims, protection = protect(spec, period, group_by, measure)
    label, unit = spec.measures[measure]
    total = _aggregate(spec, period, [], measure)
    total_value = float(total["value"].iloc[0])
    if float(total["count"].iloc[0]) < THRESHOLD:
        return ToolResult("insufficient_sample", f"조건에 맞는 값이 {THRESHOLD} 미만이라 보여 줄 수 없습니다. 조건을 넓혀 주세요.", limits=list(COMMON_LIMITS))
    rows = table.sort_values(dims) if dims and dims[0] in ("month", "quarter", "year", "hour") else table.sort_values("value", ascending=False, na_position="last")
    rows = rows.to_dict("records")
    limits = list(COMMON_LIMITS) + [spec.basis] + list(spec.limits)
    if protection["merged"]:
        limits.append(f"{THRESHOLD}명 미만인 작은 칸을 하나로 합쳤다: {', '.join(protection['merged'])}.")
    if protection["rollups"]:
        limits.append(f"{THRESHOLD}명 미만인 칸이 많아 {', '.join(protection['rollups'])} 로 더 크게 묶어 계산했다.")
    if protection["suppressedCells"]:
        limits.append(f"{THRESHOLD}명 미만이라 가린 칸이 {protection['suppressedCells']}개다. 남은 칸과 합계로 빼서 알아낼 수 없도록 {protection['extraSuppressedCells']}개를 더 가렸다.")
    else:
        limits.append("이 결과에는 가린 칸이 없다.")
    if missing:
        limits.append(f"요청 기간 중 {', '.join(missing)} 자료는 넣지 않아 빠져 있다.")
    if store.is_virtual(dataset):
        limits.insert(0, "개발·시연용 가상 데이터다. 명세 구조만 따른 값이라 실제 제주 현황이 아니다.")
    first_dim = dims[0] if dims else None
    chart = None
    if first_dim and len(rows) > 1:
        series = dims[1] if len(dims) > 1 else None
        kind = "line" if first_dim in ("month", "hour") else "bar"
        chart = {"type": kind, "x": first_dim, "y": "value", "series": series, "title": f"{spec.name} {label}"}
    return ToolResult(
        "ok",
        rows=rows,
        summary={"total": round(total_value, 1) if unit == "명" and spec.center_no == "4-15" else total_value, "groupBy": dims, "requestedGroupBy": group_by, **protection, "availablePeriod": have, "virtual": store.is_virtual(dataset)},
        chart=chart,
        evidence={
            "label": f"{spec.name} {label}",
            "unit": unit,
            "period": f"{min(wanted)}~{max(wanted)}",
            "rowCount": len(rows),
            "method": ("그 기간 모든 날짜·시각의 방문인구를 묶음별로 더해 시각 수로 나눈 평균" if spec.center_no == "4-15" else f"{label} 합계") + f", {THRESHOLD}명 미만 칸은 묶거나 가림",
            "filters": filters or {},
            "table": f"사용자가 넣은 센터 CSV ({dataset})",
        },
        limits=limits,
    )
