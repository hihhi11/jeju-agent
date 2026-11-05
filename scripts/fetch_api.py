"""전처리용 API 수집 스크립트. 원본은 data/raw/api/, 정리본 CSV는 data/processed/ 에 저장한다.

    python scripts/fetch_api.py --list
    python scripts/fetch_api.py weather_daily --probe --start 20250101 --end 20250101
    python scripts/fetch_api.py visit_purpose nationality bus_stops
    python scripts/fetch_api.py weather_daily --start 20240101 --end 20241231
    python scripts/fetch_api.py all

받은 구간은 파일로 남겨 두므로 호출 한도 등으로 중단되면 같은 명령을 다시 실행해 이어받는다.
인증키는 .env 에서만 읽는다: HUB_API_KEY, DATA_GO_KR_KEY, VISITJEJU_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

TIMEOUT = 30
RETRIES = 8  # 허브가 ESOCKETTIMEDOUT 으로 간헐적 HTTP 500을 내 4회로는 부족했다
DELAY = 0.1  # 문서상 최대 30 tps. 여유를 두고 호출한다

HUB = "https://open.jejudatahub.net/api/proxy"
KEY_ENV = {"hub": "HUB_API_KEY", "datago": "DATA_GO_KR_KEY", "visitjeju": "VISITJEJU_API_KEY"}


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


class ApiError(Exception):
    """호출 실패. 메시지에 인증키가 들어가지 않게 URL은 넣지 않는다."""


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _ym(d: date) -> str:
    return d.strftime("%Y%m")


def _names(*names: str) -> Callable[[str], bool]:
    return lambda c: c in names


def _asos_numeric(c: str) -> bool:
    return c not in {"stnId", "stnNm", "tm", "iscs"} and not c.endswith("Hrmt")


def _weather_numeric(c: str) -> bool:
    return c not in {"observatoryName", "baseDate"} and not c.endswith("Time")


def _hub_range(fmt: Callable[[date], str]) -> Callable[[date, date], dict]:
    return lambda a, b: {"startDate": fmt(a), "endDate": fmt(b)}


def _tour_params(a: date, b: date) -> dict:
    return {
        "_type": "json",
        "MobileOS": "ETC",
        "MobileApp": "JejuAgent",
        "startYmd": _ymd(a),
        "endYmd": _ymd(b),
    }


def _asos_params(a: date, b: date) -> dict:
    return {
        "dataType": "JSON",
        "dataCd": "ASOS",
        "dateCd": "DAY",
        "startDt": _ymd(a),
        "endDt": _ymd(b),
    }


def _asos_last_day() -> date:
    # 문서: 전일 자료는 조회 시각 기준 11시 이후에 조회 가능
    now = datetime.now()
    return now.date() - timedelta(days=1 if now.hour >= 11 else 2)


@dataclass(frozen=True)
class Source:
    desc: str
    api: str  # hub | datago | visitjeju
    url: str
    unit: str = "none"  # 한 파일로 받는 구간: none | day | month | year
    page_size: int = 100
    params: Callable[[date, date], dict] | None = None
    variants: tuple[dict, ...] = ({},)  # 구간마다 따로 호출할 조건 (예: 관측 지점)
    key_param: str = "serviceKey"
    default_range: tuple[str, str] | None = None  # None 이면 --start/--end 필수
    sample: tuple[str, str] | None = None  # --probe 기본 구간. 문서의 예시 값
    keep: Callable[[dict], bool] | None = None  # 응답에 지역 조건이 없는 API의 제주 필터
    numeric: Callable[[str], bool] = lambda c: False
    dates: tuple[tuple[str, str], ...] = ()  # (컬럼, 날짜 형식)
    last_day: Callable[[], date] | None = None


SOURCES = {
    "observatories": Source(
        desc="기상관측소 정보",
        api="hub",
        url=f"{HUB}/t1bab75a555a15715btaa7a5abaa1aab",
        numeric=_names("longitude", "latitude", "seaLevelAltitude"),
    ),
    "weather_daily": Source(
        desc="일별 기상 정보 (하루 1회 호출)",
        api="hub",
        url=f"{HUB}/1aD5taat1attaa51Db1511b51ab9Da19",
        unit="day",
        params=lambda a, b: {"searchDate": _ymd(a)},
        sample=("20190101", "20190101"),
        numeric=_weather_numeric,
        dates=(("baseDate", "%Y%m%d"),),
    ),
    "bus_daily": Source(
        desc="일일 정류소별 버스 이용자 (행 수가 매우 많음)",
        api="hub",
        url=f"{HUB}/1b1ta1a1ba6t6a1ttt3bD6b1tb1t1D3t",
        unit="day",
        params=_hub_range(_ymd),
        sample=("20190201", "20190201"),
        numeric=_names("userCount"),
        dates=(("baseDate", "%Y%m%d"),),
    ),
    "bus_stops": Source(
        desc="버스 정류소 기본 정보",
        api="hub",
        url=f"{HUB}/DD11ab6a6t11D16baaa1a2tD26ata161",
        numeric=_names("longitude", "latitude"),
    ),
    "nationality": Source(
        desc="국가별 입도객 현황",
        api="hub",
        url=f"{HUB}/tbata552bat15Daa585t52D5a5t818t2",
        unit="year",
        params=_hub_range(_ym),
        default_range=("202108", "202507"),
        sample=("201910", "201910"),
        numeric=_names("visitorCnt"),
        dates=(("dtYearMonth", "%Y%m"),),
    ),
    "visit_purpose": Source(
        desc="방문목적별 입도객 현황",
        api="hub",
        url=f"{HUB}/DtaDa0555D801Da508DbDtt5t1bat1ta",
        unit="year",
        params=_hub_range(_ym),
        default_range=("201801", "202507"),
        sample=("201910", "201910"),
        numeric=_names("visitorCnt"),
        dates=(("dtYearMonth", "%Y%m"),),
    ),
    "asos_daily": Source(
        desc="기상청 ASOS 일자료 (제주·고산·성산·서귀포)",
        api="datago",
        url="https://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList",
        unit="year",
        page_size=999,
        params=_asos_params,
        variants=tuple({"stnIds": stn} for stn in (184, 185, 188, 189)),
        default_range=("20180101", "20991231"),
        sample=("20240101", "20240102"),
        numeric=_asos_numeric,
        dates=(("tm", "%Y-%m-%d"),),
        last_day=_asos_last_day,
    ),
    "holidays": Source(
        desc="천문연구원 공휴일 정보",
        api="datago",
        url="https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo",
        unit="year",
        params=lambda a, b: {"_type": "json", "solYear": a.year},
        key_param="ServiceKey",
        default_range=("20180101", "20261231"),
        sample=("20240101", "20241231"),
        dates=(("locdate", "%Y%m%d"),),
    ),
    "tour_metro": Source(
        desc="관광공사 광역 지자체 방문자수 (제주만 저장)",
        api="datago",
        url="https://apis.data.go.kr/B551011/DataLabService/metcoRegnVisitrDDList",
        unit="month",
        page_size=1000,
        params=_tour_params,
        sample=("20210513", "20210513"),
        keep=lambda row: str(row.get("areaCode")) == "50",
        numeric=_names("touNum"),
        dates=(("baseYmd", "%Y%m%d"),),
    ),
    "tour_local": Source(
        desc="관광공사 기초 지자체 방문자수 (제주시·서귀포시만 저장)",
        api="datago",
        url="https://apis.data.go.kr/B551011/DataLabService/locgoRegnVisitrDDList",
        unit="month",
        page_size=1000,
        params=_tour_params,
        sample=("20210513", "20210513"),
        keep=lambda row: str(row.get("signguCode")).startswith("50"),
        numeric=_names("touNum"),
        dates=(("baseYmd", "%Y%m%d"),),
    ),
    "visitjeju": Source(
        desc="비짓제주 관광정보 콘텐츠",
        api="visitjeju",
        url="https://api.visitjeju.net/vsjApi/contents/searchList",
        numeric=_names("latitude", "longitude"),
    ),
}


def _api_key(api: str) -> str:
    key = os.getenv(KEY_ENV[api], "").strip()
    if not key:
        raise ApiError(f".env 에 {KEY_ENV[api]} 값이 없습니다")
    return key


def _parse_response(r: requests.Response, secret: str) -> dict:
    text = r.text.replace(secret, "***")
    # 공공데이터포털 게이트웨이 오류는 JSON 요청이어도 XML로 올 수 있어 본문에서 바로 찾는다
    portal = re.search(r"returnReasonCode\W+(\d+)", text)
    if portal:
        msg = re.search(r"errMsg\W+([A-Za-z_ ]+)", text)
        raise ApiError(f"포털 오류 {portal.group(1)}: {msg.group(1).strip() if msg else text[:200]}")
    if r.status_code >= 400:
        raise ApiError(f"HTTP {r.status_code}: {text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise ApiError(f"JSON 응답이 아닙니다: {text[:200]}") from None


def _get_json(url: str, params: dict, secret: str) -> dict:
    error = ""
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            error = type(e).__name__  # str(e) 에는 키가 포함된 URL이 들어 있다
        else:
            if r.status_code < 500 and r.status_code != 429:
                return _parse_response(r, secret)
            error = f"HTTP {r.status_code}"
        if attempt < RETRIES - 1:
            time.sleep(2**attempt)
    raise ApiError(f"{RETRIES}회 시도 실패 ({error})")


def _page_hub(s: Source, params: dict, page: int, size: int) -> tuple[list, bool, object]:
    key = _api_key("hub")
    body = _get_json(f"{s.url}/{key}", {**params, "number": page, "limit": size}, key)
    if "data" not in body:
        raise ApiError(f"예상과 다른 응답: {str(body)[:200]}")
    return body["data"] or [], bool(body.get("hasMore")), body.get("totCnt")


def _page_datago(s: Source, params: dict, page: int, size: int) -> tuple[list, bool, object]:
    # 포털에서 Encoding/Decoding 키 어느 쪽을 복사해도 되도록 풀어서 넘긴다 (requests가 다시 인코딩)
    key = unquote(_api_key("datago"))
    query = {s.key_param: key, **params, "pageNo": page, "numOfRows": size}
    res = _get_json(s.url, query, key).get("response", {})
    code = str(res.get("header", {}).get("resultCode", "")).strip()
    if code.isdigit() and int(code) == 3:  # NODATA_ERROR
        return [], False, 0
    if not code.isdigit() or int(code) != 0:
        raise ApiError(f"결과코드 {code}: {res.get('header', {}).get('resultMsg')}")
    body = res.get("body") or {}
    # 결과가 없으면 items 가 빈 문자열, 1건이면 item 이 리스트가 아닌 객체로 온다
    items = body.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else []
    rows = [rows] if isinstance(rows, dict) else rows
    total = int(body.get("totalCount") or 0)
    return rows, page * size < total, total


def _page_visitjeju(s: Source, params: dict, page: int, size: int) -> tuple[list, bool, object]:
    # 문서 표에는 apikey 로 적혀 있지만 소문자로 보내면 방화벽에 막힌다. apiKey 가 맞다
    key = _api_key("visitjeju")
    body = _get_json(s.url, {"apiKey": key, "locale": "kr", "page": page, **params}, key)
    if str(body.get("result")) not in {"00", "200"}:
        raise ApiError(f"결과코드 {body.get('result')}: {body.get('resultMessage')}")
    more = int(body.get("currentPage") or page) < int(body.get("pageCount") or 0)
    return body.get("items") or [], more, body.get("totalCount")


PAGERS = {"hub": _page_hub, "datago": _page_datago, "visitjeju": _page_visitjeju}


def _parse_date(text: str, end: bool = False) -> date:
    if not re.fullmatch(r"\d{6}|\d{8}", text):
        raise ValueError(f"날짜는 YYYYMM 또는 YYYYMMDD 형식이어야 합니다: {text}")
    y, m = int(text[:4]), int(text[4:6])
    if len(text) == 8:
        return date(y, m, int(text[6:]))
    return date(y, m, monthrange(y, m)[1] if end else 1)


def _chunks(unit: str, start: date, end: date) -> list[tuple[date, date]]:
    out = []
    a = start
    while a <= end:
        if unit == "day":
            b = a
        elif unit == "month":
            b = a.replace(day=monthrange(a.year, a.month)[1])
        else:
            b = a.replace(month=12, day=31)
        b = min(b, end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


def _fetch_chunk(s: Source, params: dict) -> list[dict]:
    rows: list[dict] = []
    previous = None
    page = 1
    while True:
        got, more, _ = PAGERS[s.api](s, params, page, s.page_size)
        # 페이지 번호를 무시하고 같은 내용을 돌려주는 경우 무한 반복을 막는다
        if not got or got == previous:
            break
        rows.extend(got)
        if not more:
            break
        previous = got
        page += 1
        time.sleep(DELAY)
    if s.keep:
        kept = [row for row in rows if s.keep(row)]
        if rows and not kept:
            raise ApiError(f"제주 필터에 걸린 행이 없습니다. 코드 값 확인 필요: {rows[0]}")
        return kept
    return rows


def _save_raw(path: Path, rows: list[dict]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _resolve_range(s: Source, start: str | None, end: str | None) -> tuple[date, date] | None:
    if s.unit == "none":
        return None
    default = s.default_range or (None, None)
    start, end = start or default[0], end or default[1]
    if not start or not end:
        raise ApiError("--start 와 --end 를 지정해 주세요")
    a, b = _parse_date(start), _parse_date(end, end=True)
    if s.last_day:
        b = min(b, s.last_day())
    if a > b:
        raise ApiError(f"구간이 잘못되었습니다: {a} ~ {b}")
    return a, b


def _tidy(s: Source, rows: list[dict]) -> pd.DataFrame:
    df = pd.json_normalize(rows)
    df.columns = [c.replace(".", "_") for c in df.columns]
    df = df[~df.astype(str).duplicated()].reset_index(drop=True)

    for col in df.columns:
        if s.numeric(col):
            converted = pd.to_numeric(df[col], errors="coerce")
            _warn_lost(col, df[col], converted)
            df[col] = converted
    for col, fmt in s.dates:
        if col in df:
            raw = df[col].astype(str).str.strip()
            converted = pd.to_datetime(raw, format=fmt, errors="coerce")
            _warn_lost(col, df[col], converted)
            df[col] = converted.dt.date

    # 관측소 API 문서 예시는 위도·경도가 뒤바뀌어 있다. 제주는 위도 33, 경도 126 부근이다
    if {"latitude", "longitude"} <= set(df.columns):
        swapped = df["latitude"] > df["longitude"]
        if swapped.any():
            print(f"  위도/경도가 뒤바뀐 {swapped.sum()}행을 바로잡았습니다")
            df.loc[swapped, ["latitude", "longitude"]] = df.loc[
                swapped, ["longitude", "latitude"]
            ].to_numpy()
    return df


def _warn_lost(col: str, before: pd.Series, after: pd.Series) -> None:
    filled = before.notna() & (before.astype(str).str.strip() != "")
    lost = int((filled & after.isna()).sum())
    if lost:
        print(f"  경고: {col} 값 {lost}개를 변환하지 못해 비웠습니다")


def collect(name: str, start: str | None, end: str | None, force: bool) -> Path:
    s = SOURCES[name]
    raw_dir = _path("DATA_RAW_DIR", "data/raw") / "api" / name
    out_dir = _path("DATA_PROCESSED_DIR", "data/processed")
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    span = _resolve_range(s, start, end)
    chunks = _chunks(s.unit, *span) if span else [(None, None)]
    complete_before = date.today() - timedelta(days=1)

    rows: list[dict] = []
    total = len(chunks) * len(s.variants)
    done = 0
    for a, b in chunks:
        for variant in s.variants:
            done += 1
            parts = [str(v) for v in variant.values()]
            parts += [f"{_ymd(a)}_{_ymd(b)}"] if a else ["all"]
            path = raw_dir / f"{'_'.join(parts)}.json"
            # 아직 끝나지 않은 기간은 값이 늘어날 수 있으므로 저장본을 쓰지 않는다
            finished = b is None or b < complete_before
            if path.exists() and finished and not force:
                rows.extend(json.loads(path.read_text(encoding="utf-8")))
                continue
            params = {**(s.params(a, b) if a else {}), **variant}
            got = _fetch_chunk(s, params)
            _save_raw(path, got)
            rows.extend(got)
            print(f"  [{done}/{total}] {path.stem}: {len(got)}행")
            time.sleep(DELAY)

    if not rows:
        raise ApiError("받은 데이터가 없습니다. --probe 로 제공 기간을 확인해 주세요")
    out = out_dir / f"{name}.csv"
    df = _tidy(s, rows)
    df.to_csv(out, index=False, encoding="utf-8-sig")  # 엑셀에서도 한글이 깨지지 않게 BOM 포함
    print(f"  저장: {out} ({len(df)}행, {len(df.columns)}열)")
    return out


def probe(name: str, start: str | None, end: str | None) -> None:
    """저장 없이 한 페이지만 호출해 응답 형태와 총 건수를 확인한다."""
    s = SOURCES[name]
    params: dict = {}
    if s.params:
        sample = s.sample or s.default_range or ("", "")
        a = _parse_date(start or sample[0])
        b = _parse_date(end or start or sample[1], end=True)
        params = s.params(a, b)
    params = {**params, **s.variants[0]}
    rows, more, total = PAGERS[s.api](s, params, 1, 3)
    shown = {k: v for k, v in params.items() if k not in {"_type", "MobileOS", "MobileApp"}}
    print(f"  조건 {shown} -> 총 {total}건, 다음 페이지 {more}, 이번 응답 {len(rows)}행")
    if rows:
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="전처리용 API 수집")
    parser.add_argument("sources", nargs="*", help="소스 이름 또는 all")
    parser.add_argument("--start", help="시작 YYYYMM 또는 YYYYMMDD")
    parser.add_argument("--end", help="종료 YYYYMM 또는 YYYYMMDD")
    parser.add_argument("--probe", action="store_true", help="저장 없이 응답만 확인")
    parser.add_argument("--force", action="store_true", help="저장본을 무시하고 다시 받기")
    parser.add_argument("--list", action="store_true", help="소스 목록 출력")
    args = parser.parse_args()

    if args.list or not args.sources:
        for name, s in SOURCES.items():
            period = "기간 없음" if s.unit == "none" else "기간 지정 필수"
            if s.default_range:
                period = f"기본 {s.default_range[0]}~{s.default_range[1]}"
            print(f"{name:15} {KEY_ENV[s.api]:18} {period:24} {s.desc}")
        return 0

    names = list(SOURCES) if args.sources == ["all"] else args.sources
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        parser.error(f"알 수 없는 소스: {unknown}. --list 로 확인해 주세요")

    failed = []
    for name in names:
        s = SOURCES[name]
        explicit = args.sources != ["all"] or args.probe or (args.start and args.end)
        if not explicit and s.unit != "none" and not s.default_range:
            print(f"[{name}] 건너뜀: 호출량이 많아 --start/--end 를 직접 지정해야 합니다")
            continue
        print(f"[{name}] {s.desc}")
        try:
            if args.probe:
                probe(name, args.start, args.end)
            else:
                collect(name, args.start, args.end, args.force)
        except (ApiError, ValueError) as e:
            failed.append(name)
            print(f"  실패: {e}")
            if not args.probe:
                print("  받은 구간은 저장되어 있습니다. 같은 명령을 다시 실행하면 이어받습니다")
    if failed:
        print(f"실패한 소스: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
