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
