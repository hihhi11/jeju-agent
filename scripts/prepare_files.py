"""직접 내려받은 파일 정리 스크립트. 원본은 data/raw/files/, 정리본 CSV는 data/processed/ 에 저장한다.

    python scripts/prepare_files.py --list
    python scripts/prepare_files.py spot_visitors_monthly udo_monthly
    python scripts/prepare_files.py all

컬럼 이름과 날짜 표기는 fetch_api.py 의 결과에 맞춘다: camelCase, YYYY-MM-DD, 월 자료는 1일로 표기.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

SOURCE_DIR = PROJECT_ROOT / "내가 직접 받은 데이터"
WEEKDAYS = "월화수목금토일"
# 비교 대상인 nationality.csv 의 국가 표기에 맞춘다
NATION_NAMES = {
    ("아시아", "싱가포르"): "싱가폴",
    ("아시아", "기타"): "아시아-기타",
    ("서구 등", "기타"): "서구-기타",
}


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _month_start(text: pd.Series, fmt: str) -> pd.Series:
    # 날짜를 숫자로 읽으면 2024.10 이 2024.1 이 되므로 호출하는 쪽에서 문자열로 읽어 넘긴다
    return pd.to_datetime(text, format=fmt).dt.date


def _save_df(df: pd.DataFrame, out: Path) -> None:
    temp = out.with_suffix(".tmp")
    df.to_csv(temp, index=False, encoding="utf-8-sig")  # 엑셀에서도 한글이 깨지지 않게 BOM 포함
    temp.replace(out)
    print(f"  저장: {out.name} ({len(df)}행, {len(df.columns)}열)")


def spot_visitors_monthly(srcs: list[Path], out: Path) -> None:
    frames = []
    for path, visitor_type in zip(srcs, ("내국인", "외국인")):
        df = pd.read_csv(path, encoding="cp949", dtype={"날짜": str})
        frames.append(
            pd.DataFrame(
                {
                    "dtYearMonth": _month_start(df["날짜"], "%Y.%m"),
                    "spotName": df["관광지명"],
                    "visitorType": visitor_type,
                    "visitorCnt": df["관광객수"],
                }
            )
        )
    _save_df(pd.concat(frames, ignore_index=True), out)


def spot_visitors_2024(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949")
    wide = df.pivot(index=["군구", "관광지"], columns="내외국인", values="총계").reset_index()
    mismatch = wide[wide["내국인"] + wide["외국인"] != wide["합계"]]
    if len(mismatch):
        print(f"  경고: 내국인+외국인이 합계와 다른 관광지 {len(mismatch)}곳")
    wide = wide.rename(
        columns={
            "군구": "sigungu",
            "관광지": "spotName",
            "내국인": "domesticCnt",
            "외국인": "foreignCnt",
            "합계": "totalCnt",
        }
    )
    wide.columns.name = None
    _save_df(wide, out)


def udo_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949")
    long = df.melt(id_vars=["년도", "구분"], value_vars=[f"{m}월" for m in range(1, 13)], var_name="m")
    long = long.dropna(subset=["value"])  # 아직 집계되지 않은 달
    long["dtYearMonth"] = _month_start(long["년도"].astype(str) + long["m"].str[:-1].str.zfill(2), "%Y%m")
    wide = long.pivot(index="dtYearMonth", columns="구분", values="value").astype(int).reset_index()
    wide = wide.rename(columns={"입장객": "visitorCnt", "차량": "vehicleCnt"})
    wide.columns.name = None
    _save_df(wide, out)


def domestic_floating_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"base_year_month": str})
    key = ["base_year_month", "sex", "age_range", "time_range"]
    # 같은 조건에 값이 2개인 행이 있으나 구분 컬럼이 없고 행 순서도 일정하지 않다. 임의로 합치지 않고 표시만 한다
    row_count = df.groupby(key)["visit_population_count"].transform("size")
    result = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["base_year_month"], "%Y%m"),
            "sex": df["sex"],
            "ageRange": df["age_range"],
            "timeRange": df["time_range"],
            "hour": df["time_range"].str[:2].astype(int) % 24,  # 2022.6 까지 01~24시, 이후 00~23시
            "visitPopulation": df["visit_population_count"],
            "keyRowCount": row_count,
        }
    )
    print(f"  같은 조건에 값이 2개인 행: {(row_count > 1).sum()}행")
    _save_df(result, out)


def foreign_visitors_monthly(srcs: list[Path], out: Path) -> None:
    wb = openpyxl.load_workbook(srcs[0], data_only=True)
    rows = []
    for ws in wb.worksheets:
        if not ws.title.endswith("월"):
            continue
        month = int(ws.title[:-1])
        group = name = None
        for row in ws.iter_rows(min_row=5, values_only=True):
            group, name = row[0] or group, row[1] or name
            if row[2] != "월계":  # 누계는 월계에서 계산할 수 있어 저장하지 않는다
                continue
            label = NATION_NAMES.get((group, name), name) if group != "총계" else "총계"
            for year, jeju, korea in ((2025, row[3], row[6]), (2024, row[4], row[7])):
                rows.append(
                    {
                        "dtYearMonth": date(year, month, 1),
                        "nationality": label,
                        "group": group,
                        "jejuVisitorCnt": int(jeju),
                        "koreaVisitorCnt": int(korea),
                    }
                )
    df = pd.DataFrame(rows)
    _check_foreign_totals(df)
    keep = df[~df["nationality"].isin(["총계", "소계"])].drop(columns="group")
    _save_df(keep.sort_values(["dtYearMonth"], kind="stable").reset_index(drop=True), out)


def _check_foreign_totals(df: pd.DataFrame) -> None:
    nations = df[~df["nationality"].isin(["총계", "소계"])]
    totals = df[df["nationality"] == "총계"].set_index("dtYearMonth").sort_index()
    summed = nations.groupby("dtYearMonth")[["jejuVisitorCnt", "koreaVisitorCnt"]].sum()
    for col in summed:
        bad = (summed[col] != totals[col]).sum()
        if bad:
            print(f"  경고: {col} 국가 합계와 총계가 다른 달 {bad}개")
    api_path = _path("DATA_PROCESSED_DIR", "data/processed") / "nationality.csv"
    if api_path.exists():
        api = pd.read_csv(api_path, encoding="utf-8-sig", parse_dates=["dtYearMonth"])
        api["dtYearMonth"] = api["dtYearMonth"].dt.date
        merged = nations.merge(api, on=["dtYearMonth", "nationality"])
        bad = merged[merged["jejuVisitorCnt"] != merged["visitorCnt"]]
        print(f"  국가별 입도객 API와 대조: {len(merged)}건 중 불일치 {len(bad)}건")


def airport_passengers_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"기준일(월)": str})
    key = ["기준일(월)", "공항", "도착출발구분"]
    # 원본은 2024-08 이후의 모든 행이 두 번씩 들어 있다. 값이 다르면 어느 쪽이 맞는지 알 수 없으므로 멈춘다
    conflict = df.groupby(key)["이용자수(명)"].nunique().gt(1).sum()
    if conflict:
        raise ValueError(f"{srcs[0].name}: 같은 조건에 값이 다른 행 {conflict}개")
    unique = df.drop_duplicates()
    print(f"  중복 제거: {len(df) - len(unique)}행")
    result = pd.DataFrame(
        {
            "dtYearMonth": _month_start(unique["기준일(월)"], "%Y%m"),
            "airport": unique["공항"],
            "direction": unique["도착출발구분"],
            "passengerCnt": unique["이용자수(명)"],
        }
    )
    _save_df(result, out)


def ferry_passengers_yearly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949")
    # 일반+섬주민이 합계와 맞지 않는 행이 있어 합계만 쓴다
    result = pd.DataFrame(
        {
            "baseYear": df["기준일(년)"],
            "shipName": df["선박명"],
            "route": df["항로"],
            "routeType": df["항로"].str.startswith("제주-").map({True: "제주-뭍", False: "도내"}),
            "capacity": df["여객정원"],
            "passengerCnt": df["여객수송실적(합계)"],
        }
    )
    _save_df(result, out)


def _gaps(days: list[date]) -> str:
    out = []
    for a, b in zip(days, days[1:]):
        if b - a > timedelta(days=1):
            out.append(f"{a + timedelta(days=1)}~{b - timedelta(days=1)}")
    return ", ".join(out) or "없음"


def _convert_bus(
    src: Path,
    out: Path,
    columns: list[str],
    free_col: int | None = None,
    check: Callable[[date, list[str]], bool] | None = None,
) -> None:
    """행 수가 많아 줄 단위로 변환한다. 쉼표가 든 정류소 이름이 따옴표 없이 저장되어 있어 CSV 파서를 쓸 수 없다."""
    n = len(columns)
    temp = out.with_suffix(".tmp")
    rows = joined = dup_keys = failed = 0
    total = 0
    days: list[date] = []
    current = ""
    keys: set[tuple[str, ...]] = set()
    with (
        open(src, encoding="utf-8-sig", newline="") as fin,
        open(temp, "w", encoding="utf-8-sig", newline="") as fout,
    ):
        writer = csv.writer(fout)
        writer.writerow(columns)
        next(fin)
        for line in fin:
            parts = line.rstrip("\r\n").split(",")
            extra = len(parts) - n
            if extra and free_col is not None and extra > 0:
                parts[free_col : free_col + extra + 1] = [",".join(parts[free_col : free_col + extra + 1])]
                joined += 1
            elif extra:
                raise ValueError(f"{src.name}: 필드 수가 맞지 않습니다: {line[:100]}")
            raw = parts[0]
            if raw != current:
                day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
                if days and day <= days[-1]:
                    raise ValueError(f"{src.name}: 날짜가 정렬되어 있지 않습니다: {raw}")
                days.append(day)
                current = raw
                iso = day.isoformat()
                keys.clear()
            key = tuple(parts[:-1])
            if key in keys:
                dup_keys += 1
            keys.add(key)
            if check and not check(days[-1], parts):
                failed += 1
            total += int(parts[-1])
            parts[0] = iso
            writer.writerow(parts)
            rows += 1
    temp.replace(out)
    print(f"  저장: {out.name} ({rows}행, {n}열), userCount 합계 {total}")
    print(f"  기간 {days[0]}~{days[-1]}, {len(days)}일, 빠진 구간: {_gaps(days)}")
    if joined:
        print(f"  이름의 쉼표를 복원한 행: {joined}행")
    if dup_keys:
        print(f"  경고: 같은 날 같은 조건이 중복된 행 {dup_keys}행")
    if failed:
        print(f"  경고: 검사 실패 {failed}행")


def bus_stop_user_daily(srcs: list[Path], out: Path) -> None:
    _convert_bus(srcs[0], out, ["baseDate", "stationName", "userType", "userCount"], free_col=1)


def bus_route_daily(srcs: list[Path], out: Path) -> None:
    _convert_bus(srcs[0], out, ["baseDate", "actType", "busNumber", "userType", "userCount"])


def bus_hourly(srcs: list[Path], out: Path) -> None:
    _convert_bus(
        srcs[0],
        out,
        ["baseDate", "dayOfWeek", "baseHour", "userType", "userCount"],
        check=lambda day, parts: WEEKDAYS[day.weekday()] == parts[1],
    )


def card_industry_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"base_year_month": str})
    frame = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["base_year_month"], "%Y%m"),
            # 원본의 일부 달에 업종 이름이 '기타음?식료품위주종합소매업' 으로 깨져 있다
            "industry": df["biz_nm"].str.replace("?", "", regex=False),
            "userType": df["user_type"],
            "sex": df["sex"],
            "cardUsers": df["user_count"],
            "useCount": df["use_count"],
            "useAmount": df["use_amount"],
        }
    )
    _save_df(frame, out)


def card_time_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"base_year_month": str})
    frame = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["base_year_month"], "%Y%m"),
            "timeSlot": df["time_type"],
            "userType": df["user_type"],
            "ageRange": df["age_range"],
            "useAmountMillion": df["use_amout_by_million"],
        }
    )
    _save_df(frame, out)


def menu_sales_monthly(srcs: list[Path], out: Path) -> None:
    # 메뉴 26개는 센터 명세 (참고표2) 메뉴코드표의 대분류와 같다. 원본은 CP949, 금액은 백만 원 단위다
    df = pd.read_csv(srcs[0], encoding="cp949", dtype={"base_year_month": str})
    frame = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["base_year_month"], "%Y%m"),
            "menu": df["menu_name"].str.strip(),
            "salesAmountMillion": df["sale_amount_by_million"],
        }
    )
    _save_df(frame, out)


BSI_REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주")


def sbiz_bsi_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949")
    # 전국 값은 원본의 경기전반 열이다. 시도 열은 업종을 나누지 않은 지역 값이다
    regions = {"전국": "경기전반", **{r: r for r in BSI_REGIONS}}
    month = _month_start(df["연도"].astype(str) + df["월"].astype(str).str.zfill(2), "%Y%m")
    parts = []
    for region, prefix in regions.items():
        for outlook in ("체감", "전망"):
            parts.append(pd.DataFrame({"dtYearMonth": month, "region": region, "outlook": outlook, "bsi": df[f"{prefix}{outlook}"]}))
    frame = pd.concat(parts, ignore_index=True)
    blank = frame["bsi"].isna()
    print(f"  빈 값 {blank.sum()}개는 뺀다 (제주 체감 {int((blank & (frame['region'] == '제주') & (frame['outlook'] == '체감')).sum())}개)")
    _save_df(frame[~blank], out)


def restaurants(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949", dtype=str)
    jibun = df["소재지전체주소"].fillna("").str.split()
    road = df["도로명전체주소"].fillna("").str.split()
    # 지번 주소가 없으면 도로명 주소의 읍면이나 괄호 안 동 이름을 쓴다
    road_area = road.str[2].where(road.str[2].fillna("").str.endswith(("읍", "면")), df["도로명전체주소"].str.extract(r"\(([^,)]+)")[0])
    frame = pd.DataFrame(
        {
            "province": jibun.str[0].where(jibun.str.len() > 0, road.str[0]),
            "sigungu": jibun.str[1].where(jibun.str.len() > 0, road.str[1]),
            "area": jibun.str[2].where(jibun.str.len() > 0, road_area),
            "businessType": df["업종구분대분류"],
            "category": df["업종구분소분류"],
            "status": df["영업상태명"],
            "licenseDate": pd.to_datetime(df["인허가일자"], format="%Y%m%d").dt.date,
            # 원본의 폐업일자는 20200101.0 처럼 소수로 적혀 있다
            "closeDate": pd.to_datetime(df["폐업일자"].str.removesuffix(".0"), format="%Y%m%d").dt.date,
        }
    )
    outside = frame["province"] != "제주특별자치도"
    print(f"  제주 밖 주소 {outside.sum()}행은 뺀다, 지역을 정하지 못한 행 {frame['area'].isna().sum()}행")
    _save_df(frame[~outside].drop(columns="province"), out)


def jeju_events(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="cp949", dtype=str)
    frame = pd.DataFrame(
        {
            "eventName": df["콘텐츠명"].str.strip(),
            "startDate": pd.to_datetime(df["시작일"], errors="coerce").dt.date,
            "endDate": pd.to_datetime(df["종료일"], errors="coerce").dt.date,
            "startTime": df["시작시간"],
            "endTime": df["종료시간"],
            "fee": df["요금"],
            "host": df["주관"],
            "organizer": df["주최"],
        }
    )
    no_date = frame["startDate"].isna() | frame["endDate"].isna()
    reversed_ = ~no_date & (frame["endDate"] < frame["startDate"])
    print(f"  날짜 없는 행 {no_date.sum()}행, 종료일이 시작일보다 이른 행 {reversed_.sum()}행은 뺀다")
    _save_df(frame[~no_date & ~reversed_], out)


def tourist_spending_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"기준연월": str})
    items = [c for c in df.columns if c not in ("기준연월", "전체")]
    # 항목마다 원 단위로 반올림해 합계가 1원쯤 어긋나는 달이 있다
    diff = (df[items].sum(axis=1) - df["전체"]).abs()
    if diff.max() > 2:
        raise ValueError(f"{srcs[0].name}: 항목 합계가 전체와 다릅니다 (최대 {diff.max()}원)")
    # 전체는 항목의 합이라 빼고, 필요하면 도구가 항목을 더한다
    frame = df.melt(id_vars="기준연월", value_vars=items, var_name="item", value_name="amount")
    frame.insert(0, "dtYearMonth", _month_start(frame.pop("기준연월"), "%Y-%m"))
    print(f"  항목 {len(items)}개의 합과 전체의 차이: 최대 {diff.max()}원, 다른 달 {(diff > 0).sum()}개")
    _save_df(frame, out)


# 제주관광빅데이터플랫폼(data.ijto.or.kr) 에서 받은 파일. 내려받은 날(2026-11-06) 기준으로 진행 중인 달은 뺀다
PLATFORM_LAST_MONTH = date(2026, 10, 1)
SIGUNGU_CODE = {"3901": "제주시", "3902": "서귀포시", "50110": "제주시", "50130": "서귀포시"}


def arrivals_long_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_excel(srcs[0], sheet_name="분석용 데이터")
    wide = df.pivot_table(index=["년도", "월"], columns="구분", values="방문객수", aggfunc="sum").reset_index()
    if not (wide["전체"] == wide["내국인"] + wide["외국인"]).all():
        raise ValueError(f"{srcs[0].name}: 전체가 내국인+외국인과 다른 달이 있습니다")
    frame = pd.DataFrame(
        {
            "dtYearMonth": pd.to_datetime(dict(year=wide["년도"], month=wide["월"], day=1)).dt.date,
            "domesticCnt": wide["내국인"].astype(int),
            "foreignCnt": wide["외국인"].astype(int),
        }
    )
    _save_df(frame.sort_values("dtYearMonth"), out)


def cruise_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"입항년월(YYYYMM)": str})
    if not (df["총승객수"] == df["한국인승객수"] + df["외국인승객수"]).all():
        raise ValueError(f"{srcs[0].name}: 총승객수가 한국인+외국인과 다른 행이 있습니다")
    month = _month_start(df["입항년월(YYYYMM)"], "%Y%m")
    parts = [pd.DataFrame({"dtYearMonth": month, "ship": df["선박명"], "passengerType": kind, "passengerCnt": df[col]}) for kind, col in (("한국인", "한국인승객수"), ("외국인", "외국인승객수"))]
    frame = pd.concat(parts, ignore_index=True)
    late = frame["dtYearMonth"] > PLATFORM_LAST_MONTH
    print(f"  진행 중인 달(2026-11 이후) {late.sum() // 2}행은 뺀다, 승객 0명인 입항 {int((df['총승객수'] == 0).sum())}행")
    _save_df(frame[~late], out)


def rental_car_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"기준연월(YYYYMM)": str})
    # 원본 2개월(2023-10 1시간, 2024-09 60시간)은 총 대여 시간이 승용+승합보다 작다. 총계는 원본 값을 둔다
    gap = df["승용차 대여 시간"] + df["승합차 대여 시간"] - df["총 대여 시간"]
    if gap.abs().max() > 100:
        raise ValueError(f"{srcs[0].name}: 총 대여 시간이 승용+승합과 크게 다른 달이 있습니다 (최대 {gap.abs().max()}시간)")
    print(f"  총 대여 시간과 승용+승합이 다른 달 {(gap != 0).sum()}개 (최대 {gap.abs().max()}시간)")
    frame = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["기준연월(YYYYMM)"], "%Y%m"),
            "fleetCnt": df["등록대수"],
            "totalRentHours": df["총 대여 시간"],
            "carRentHours": df["승용차 대여 시간"],
            "vanRentHours": df["승합차 대여 시간"],
            "utilizationPct": df["가동률(%)"],
        }
    )
    _save_df(frame.sort_values("dtYearMonth"), out)


def vehicle_arrivals_monthly(srcs: list[Path], out: Path) -> None:
    df = pd.read_csv(srcs[0], encoding="utf-8-sig", dtype={"주행연월(YYYYMM)": str, "읍면동코드": str})
    frame = pd.DataFrame(
        {
            "dtYearMonth": _month_start(df["주행연월(YYYYMM)"], "%Y%m"),
            "sigungu": df["읍면동코드"].str[:4].map(SIGUNGU_CODE),
            "area": df["읍면동명"],
            "arrivalCnt": df["도착건수"],
        }
    )
    if frame["sigungu"].isna().any():
        raise ValueError(f"{srcs[0].name}: 시를 정하지 못한 읍면동 코드가 있습니다")
    _save_df(frame.sort_values(["dtYearMonth", "area"]), out)


@dataclass(frozen=True)
class Dataset:
    desc: str
    files: tuple[str, ...]
    convert: Callable[[list[Path], Path], None]


DATASETS = {
    "spot_visitors_monthly": Dataset(
        "관광지별 월별 입장객 (내국인·외국인, 50곳)",
        ("제주특별자치도_내국인관광지입장객수추이.csv", "제주특별자치도_외국인관광지입장객수추이.csv"),
        spot_visitors_monthly,
    ),
    "spot_visitors_2024": Dataset(
        "주요 관광지점 2024년 입장객 (36곳, 시군 구분 포함)",
        ("제주특별자치도_주요관광지점 입장객 통계_20241231.csv",),
        spot_visitors_2024,
    ),
    "udo_monthly": Dataset(
        "우도해양도립공원 월별 입장객·차량",
        ("제주특별자치도 제주시_우도해양도립공원입장객현황_20260313.csv",),
        udo_monthly,
    ),
    "domestic_floating_monthly": Dataset(
        "제주 방문 내국인 유동인구 (월·성별·연령·시간대)",
        ("제주도방문내국인유동인구추이.csv",),
        domestic_floating_monthly,
    ),
    "foreign_visitors_monthly": Dataset(
        "외국인 관광객 입도 통계 2024~2025 (국가별, 제주·전국)",
        ("제주관광공사_제주방문관광객_통계_20251231.xlsx",),
        foreign_visitors_monthly,
    ),
    "airport_passengers_monthly": Dataset(
        "국내 공항별 월별 이용자 (도착·출발)",
        ("국내 공항별 운항 통계(이용자).csv",),
        airport_passengers_monthly,
    ),
    "ferry_passengers_yearly": Dataset(
        "제주 여객선 연간 수송 인원 (선박·항로별)",
        ("제주특별자치도여객선수송통계(여객).csv",),
        ferry_passengers_yearly,
    ),
    "card_industry_monthly": Dataset(
        "업종별 성별 카드 이용 (월·업종·이용자 유형)",
        ("업종별성별카드이용정보.csv",),
        card_industry_monthly,
    ),
    "card_time_monthly": Dataset(
        "시간대별 카드 이용금액 (월·시간대·이용자 유형·연령)",
        ("시간대별카드이용금액현황.csv",),
        card_time_monthly,
    ),
    "menu_sales_monthly": Dataset(
        "메뉴별 외식업 매출액 (월·메뉴 대분류 26개)",
        ("메뉴별외식업매출액.csv",),
        menu_sales_monthly,
    ),
    "sbiz_bsi_monthly": Dataset(
        "소상공인 경기동향 BSI (월·지역·체감/전망)",
        ("소상공인시장진흥공단_소상공인 경기동향(BSI) 현황_20250620.csv",),
        sbiz_bsi_monthly,
    ),
    "restaurants": Dataset(
        "제주 음식점 인허가·폐업 목록 (일반·휴게음식점)",
        ("제주특별자치도음식점목록(통합).csv",),
        restaurants,
    ),
    "jeju_events": Dataset(
        "제주 주요 행사 (이름·기간)",
        ("제주특별자치도주요행사정보.csv",),
        jeju_events,
    ),
    "tourist_spending_monthly": Dataset(
        "제주 방문 내국인 관광객 1인당 지출 경비 (월·항목)",
        ("제주관광공사_제주방문관광객 실태조사 내국인 관광객 1인당 지출 경비 통계_20260401.csv",),
        tourist_spending_monthly,
    ),
    "arrivals_long_monthly": Dataset(
        "월별 입도 관광객 2001~ (내국인·외국인, 제주관광빅데이터플랫폼)",
        ("입도관광객 전체_2026년08월 기준.xlsx",),
        arrivals_long_monthly,
    ),
    "cruise_monthly": Dataset(
        "월별 크루즈 입도 (선박별 한국인·외국인 승객, 제주관광빅데이터플랫폼)",
        ("월별 크루즈 입도 통계_2024-01~2026-11.csv",),
        cruise_monthly,
    ),
    "rental_car_monthly": Dataset(
        "렌터카 운영 현황 (등록대수, 대여 시간, 가동률, 제주관광빅데이터플랫폼)",
        ("렌터카 운영 현황_2023-01~2025-10.csv",),
        rental_car_monthly,
    ),
    "vehicle_arrivals_monthly": Dataset(
        "월별 읍면동 차량 도착 건수 (티맵, 제주관광빅데이터플랫폼)",
        ("월별 읍면동 차량 도착_2021-01~2026-10.csv",),
        vehicle_arrivals_monthly,
    ),
    "bus_hourly": Dataset(
        "일별 시간대별 버스 승차 인원",
        ("일별시간대별버스승차인원수현황.csv",),
        bus_hourly,
    ),
    "bus_route_daily": Dataset(
        "일별 노선별 버스 승차·하차·환승 인원 (행 수가 매우 많음)",
        ("일별버스승차인원수현황.csv",),
        bus_route_daily,
    ),
    "bus_stop_user_daily": Dataset(
        "일별 정류소별 이용자 유형별 인원 (행 수가 매우 많음)",
        ("이용자유형별버스정류소이용인원현황.csv",),
        bus_stop_user_daily,
    ),
}


def _copy_raw(name: str, raw_dir: Path) -> Path:
    src, dst = SOURCE_DIR / name, raw_dir / name
    # 받은 폴더를 지운 뒤에는 raw/files 사본을 그대로 쓴다
    if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size):
        temp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, temp)
        temp.replace(dst)
    return dst


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="직접 내려받은 파일 정리")
    parser.add_argument("datasets", nargs="*", help="데이터 이름 또는 all")
    parser.add_argument("--list", action="store_true", help="데이터 목록 출력")
    args = parser.parse_args()

    if args.list or not args.datasets:
        for name, d in DATASETS.items():
            print(f"{name:27} {d.desc}")
        return 0

    names = list(DATASETS) if args.datasets == ["all"] else args.datasets
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        parser.error(f"알 수 없는 데이터: {unknown}. --list 로 확인해 주세요")

    raw_dir = _path("DATA_RAW_DIR", "data/raw") / "files"
    out_dir = _path("DATA_PROCESSED_DIR", "data/processed")
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in names:
        d = DATASETS[name]
        print(f"[{name}] {d.desc}")
        d.convert([_copy_raw(f, raw_dir) for f in d.files], out_dir / f"{name}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
