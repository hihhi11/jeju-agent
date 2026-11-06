"""data/processed/ 의 CSV와 reference/ 의 참조 파일로 로컬 DuckDB를 만드는 스크립트.

    python scripts/build_db.py

버스 원본은 행이 많아 집계해서 넣는다. bus_daily 는 2026-10까지 온전한 달로, tour_local 은 CSV가 있어야 만들어진다.
결과가 원본과 맞지 않으면 예외를 내고 기존 DB를 건드리지 않는다.
허브 목록은 배포가 금지되어 이 DB에 넣지 않는다 (scripts/hub_list.py 가 받은 파일을 직접 읽는다).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import openpyxl
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

REFERENCE_DIR = PROJECT_ROOT / "reference"
CENTER_SPEC = PROJECT_ROOT / "데이터명세서_kr_v13.xlsx"
# 노선 수가 앞뒤 14일 중앙값의 이 범위를 벗어나면 원본 일부 누락·중복으로 본다
CARD_RATIO_LOW = 0.97
CARD_RATIO_HIGH = 1.01
ROUTE_RATIO_LOW = 0.7
ROUTE_RATIO_HIGH = 1.5
# bus_daily.csv 는 마지막에 받은 기간으로 덮어써지므로 어느 달 1일부터 이 날까지 빠짐없는지 확인한다
BUS_STATION_END = "2026-10-31"
# 이용 인원이 그 달 평일·휴일별 중앙값의 이 비율 미만이면 원본 일부 누락으로 본다
BUS_STATION_RATIO_LOW = 0.7
# 같은 이름의 정류소가 이 값(도, 약 3km) 이상 떨어져 있으면 위치를 정하지 않는다
STOP_SPREAD_DEGREE = 0.03
# 음식점 목록은 2025-09-02 갱신본이다. 폐업 기록이 1994년부터라 그 전에 문을 닫은 곳은 빠졌을 수 있어
# 2000년대 초 폐업 건수가 적어(카페 2000~2002년 1~26건) 2010년부터 세고, 마지막 달은 갱신 직전 달(2025-08)까지 쓴다
RESTAURANT_START = "2010-01-01"
RESTAURANT_END = "2025-09-01"
# 원본 소분류를 사용자가 부르는 말로 묶는다. 이 프로젝트가 정한 묶음이고 나머지는 기타다
FOOD_GROUPS = {
    "카페": ("커피숍", "까페", "다방", "전통찻집", "떡카페"),
    "제과·아이스크림": ("과자점", "아이스크림"),
    "주점": ("호프/통닭", "정종/대포집/소주방", "감성주점", "라이브카페"),
    "치킨": ("통닭(치킨)",),
    "한식": ("한식", "탕류(보신용)", "냉면집"),
    "고기구이": ("식육(숯불구이)",),
    "횟집": ("횟집", "복어취급"),
    "일식": ("일식",),
    "중식": ("중국식",),
    "양식": ("경양식", "패밀리레스트랑"),
    "외국음식": ("외국음식전문점(인도,태국등)",),
    "분식": ("분식", "김밥(도시락)"),
    "패스트푸드": ("패스트푸드",),
    "편의점": ("편의점",),
}
NAME_PUNCTUATION =r"[\s\-\.·,()\[\]]"
NAME_KEY_SQL = f"upper(regexp_replace({{0}}, '{NAME_PUNCTUATION}', '', 'g'))"


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _load_base_tables(con: duckdb.DuckDBPyConnection, src) -> None:
    con.execute(f"CREATE TABLE spot_visitors_monthly AS SELECT * FROM {src('spot_visitors_monthly')}")
    con.execute(f"CREATE TABLE spot_visitors_2024 AS SELECT * FROM {src('spot_visitors_2024')}")
    # 여행형태 3종과 방문목적 6종은 같은 사람을 두 기준으로 나눈 값이라 전체를 더하면 2배가 된다
    con.execute(f"""
        CREATE TABLE visit_purpose AS
        SELECT dtYearMonth,
               CASE WHEN visitPurpose IN ('개별여행', '패키지', '부분패키지') THEN '여행형태' ELSE '방문목적' END AS classType,
               visitPurpose, visitorCnt
        FROM {src('visit_purpose')}
    """)
    con.execute(f"CREATE TABLE udo_monthly AS SELECT * FROM {src('udo_monthly')}")
    con.execute(f"CREATE TABLE airport_passengers_monthly AS SELECT * FROM {src('airport_passengers_monthly')}")
    con.execute(f"CREATE TABLE ferry_passengers_yearly AS SELECT * FROM {src('ferry_passengers_yearly')}")
    con.execute(f"""
        CREATE TABLE domestic_floating_monthly AS
        SELECT *, CASE WHEN dtYearMonth < DATE '2022-07-01' THEN '2022-06까지' ELSE '2022-07부터' END AS regime
        FROM {src('domestic_floating_monthly')}
    """)
    con.execute(f"""
        CREATE TABLE tour_metro_daily AS
        SELECT baseYmd AS baseDate, regexp_replace(touDivNm, '\\(.*\\)', '') AS visitorClass, touNum AS visitors
        FROM {src('tour_metro')}
    """)
    con.execute(f"""
        CREATE TABLE visitjeju AS
        SELECT contentsid AS contentsId, title, contentscd_label AS contentType, region1cd_label AS region1,
               region2cd_label AS region2, address, roadaddress AS roadAddress,
               TRY_CAST(latitude AS DOUBLE) AS latitude, TRY_CAST(longitude AS DOUBLE) AS longitude,
               tag, alltag AS allTag, introduction, phoneno AS phoneNo
        FROM read_csv('{src.path('visitjeju')}', all_varchar = true)
    """)
    con.execute(f"""
        CREATE TABLE observatories AS
        SELECT observatoryCode, observatoryName, startDate, endDate, managementOffice,
               longitude, latitude, seaLevelAltitude
        FROM {src('observatories')}
    """)
    con.execute(f"""
        CREATE TABLE bus_stops AS
        SELECT *, NULLIF(regexp_extract(stationAddress, '(제주시|서귀포시)'), '') AS sigungu FROM {src('bus_stops')}
    """)


def _load_card(con: duckdb.DuckDBPyConnection, src) -> None:
    # 내국인은 제주도민이 따로 있어 도민이 아닌 내국인으로 추정한다. 제공처 정의는 찾지 못했다
    group = """CASE userType WHEN '제주도민' THEN '제주도민' WHEN '법인' THEN '법인'
                WHEN '내국인' THEN '관광객(추정)' WHEN '중국' THEN '관광객(추정)' WHEN '일본' THEN '관광객(추정)'
                WHEN '동남아' THEN '관광객(추정)' WHEN '기타외국' THEN '관광객(추정)' END"""
    con.execute(f"""
        CREATE TABLE card_industry_monthly AS
        SELECT dtYearMonth, industry, userType, {group} AS spenderGroup, sex, cardUsers, useCount, useAmount
        FROM {src('card_industry_monthly')}
    """)
    con.execute(f"""
        CREATE TABLE card_time_monthly AS
        SELECT dtYearMonth, timeSlot, userType, {group} AS spenderGroup, ageRange, useAmountMillion * 1000000 AS useAmount
        FROM {src('card_time_monthly')}
    """)


def _load_local_business(con: duckdb.DuckDBPyConnection, src) -> None:
    con.execute(f"CREATE TABLE sbiz_bsi_monthly AS SELECT * FROM {src('sbiz_bsi_monthly')}")
    groups = " ".join(f"WHEN category IN ({', '.join(repr(c) for c in cats)}) THEN '{name}'" for name, cats in FOOD_GROUPS.items())
    con.execute(f"CREATE TABLE restaurants AS SELECT *, CASE {groups} ELSE '기타' END AS foodGroup FROM {src('restaurants')}")
    # 인허가와 폐업을 달별 건수로 센다. 0건인 달은 도구가 채운다
    con.execute(f"""
        CREATE TABLE restaurant_monthly AS
        SELECT date_trunc('month', d)::DATE AS dtYearMonth, eventType, sigungu, area, businessType, category, foodGroup, count(*) AS cnt
        FROM (SELECT licenseDate AS d, '인허가' AS eventType, * FROM restaurants
              UNION ALL SELECT closeDate, '폐업', * FROM restaurants WHERE closeDate IS NOT NULL)
        WHERE d >= DATE '{RESTAURANT_START}' AND d < DATE '{RESTAURANT_END}'
        GROUP BY ALL
    """)
    con.execute(f"CREATE TABLE tourist_spending_monthly AS SELECT * FROM {src('tourist_spending_monthly')}")
    con.execute(f"CREATE TABLE jeju_events AS SELECT *, datediff('day', startDate, endDate) + 1 AS durationDays FROM {src('jeju_events')}")


def _load_platform(con: duckdb.DuckDBPyConnection, src) -> None:
    """제주관광빅데이터플랫폼에서 받은 크루즈, 렌터카, 차량 도착, 읍면동 방문객."""
    con.execute(f"CREATE TABLE cruise_monthly AS SELECT * FROM {src('cruise_monthly')}")
    con.execute(f"CREATE TABLE rental_car_monthly AS SELECT * FROM {src('rental_car_monthly')}")
    con.execute("""
        CREATE VIEW rental_hours_monthly AS
        SELECT dtYearMonth, '승용차' AS carType, carRentHours AS rentHours FROM rental_car_monthly
        UNION ALL SELECT dtYearMonth, '승합차', vanRentHours FROM rental_car_monthly
    """)
    con.execute(f"CREATE TABLE vehicle_arrivals_monthly AS SELECT * FROM {src('vehicle_arrivals_monthly')}")


def _load_foreign_arrivals(con: duckdb.DuckDBPyConnection, src) -> None:
    # 겹치는 달은 값이 같아야 한다. 허브 API를 기준으로 쓰고 이후 달을 제주관광공사 통계로 잇는다
    bad = con.execute(f"""
        SELECT count(*) FROM {src('nationality')} n
        JOIN {src('foreign_visitors_monthly')} f USING (dtYearMonth, nationality)
        WHERE n.visitorCnt <> f.jejuVisitorCnt
    """).fetchone()[0]
    if bad:
        raise ValueError(f"국가별 입도객 API와 제주관광공사 통계가 다른 행 {bad}개")
    con.execute(f"""
        CREATE TABLE foreign_arrivals_monthly AS
        SELECT dtYearMonth, nationality, COALESCE(n.visitorCnt, f.jejuVisitorCnt) AS jejuVisitorCnt,
               f.koreaVisitorCnt
        FROM {src('nationality')} n
        FULL JOIN {src('foreign_visitors_monthly')} f USING (dtYearMonth, nationality)
        ORDER BY dtYearMonth, nationality
    """)
    con.execute("""
        CREATE VIEW arrivals_hub_monthly AS
        WITH domestic AS (
            SELECT dtYearMonth, sum(visitorCnt) AS domesticCnt
            FROM visit_purpose WHERE classType = '여행형태' GROUP BY dtYearMonth
        ), foreigners AS (
            SELECT dtYearMonth, sum(jejuVisitorCnt) AS foreignCnt FROM foreign_arrivals_monthly GROUP BY dtYearMonth
        )
        SELECT dtYearMonth, domesticCnt, foreignCnt, domesticCnt + foreignCnt AS totalCnt
        FROM domestic JOIN foreigners USING (dtYearMonth)
    """)
    con.execute(f"""
        CREATE TABLE arrivals_monthly AS
        SELECT dtYearMonth, domesticCnt, foreignCnt, domesticCnt + foreignCnt AS totalCnt
        FROM {src('arrivals_long_monthly')} ORDER BY dtYearMonth
    """)


def _load_spots(con: duckdb.DuckDBPyConnection) -> None:
    spots_csv = (REFERENCE_DIR / "spots.csv").as_posix()
    con.execute(f"""
        CREATE TABLE dim_spot AS
        WITH facts AS (
            SELECT spotName, min(dtYearMonth) AS firstMonth, max(dtYearMonth) AS lastMonth,
                   sum(visitorCnt) FILTER (WHERE year(dtYearMonth) = 2024) AS total2024
            FROM spot_visitors_monthly GROUP BY spotName
        )
        SELECT r.spotName, r.sigungu, r.sigunguSource, r.sigunguConfidence, r.category,
               f.firstMonth, f.lastMonth, f.total2024
        FROM read_csv('{spots_csv}', all_varchar = true) r JOIN facts f USING (spotName)
        ORDER BY r.spotName
    """)
    con.execute(f"""
        CREATE TABLE spot_alias AS
        SELECT DISTINCT alias, spotName FROM (
            SELECT replace(spotName, ' ', '') AS alias, spotName FROM read_csv('{spots_csv}', all_varchar = true)
            UNION ALL
            SELECT replace(unnest(string_split(aliases, '|')), ' ', ''), spotName
            FROM read_csv('{spots_csv}', all_varchar = true) WHERE aliases IS NOT NULL
        )
        ORDER BY alias
    """)
    con.execute("""
        CREATE VIEW spot_visitors_view AS
        SELECT v.dtYearMonth, year(v.dtYearMonth) AS year, month(v.dtYearMonth) AS month, v.spotName,
               d.sigungu, d.category, v.visitorType, v.visitorCnt
        FROM spot_visitors_monthly v JOIN dim_spot d USING (spotName)
    """)


def _load_calendar_and_weather(con: duckdb.DuckDBPyConnection, src) -> None:
    con.execute(f"""
        CREATE TABLE dim_date AS
        WITH days AS (
            SELECT unnest(generate_series(DATE '2018-01-01', DATE '2026-12-31', INTERVAL 1 DAY))::DATE AS baseDate
        ), holidays AS (
            SELECT locdate AS baseDate, string_agg(dateName, ', ') AS holidayName
            FROM {src('holidays')} WHERE isHoliday = 'Y' GROUP BY locdate
        )
        SELECT baseDate, year(baseDate) AS year, month(baseDate) AS month, day(baseDate) AS day,
               substr('월화수목금토일', isodow(baseDate)::INT, 1) AS dayOfWeek,
               isodow(baseDate) >= 6 AS isWeekend,
               holidayName IS NOT NULL AS isHoliday, holidayName,
               isodow(baseDate) >= 6 OR holidayName IS NOT NULL AS isOffDay
        FROM days LEFT JOIN holidays USING (baseDate)
        ORDER BY baseDate
    """)
    con.execute(f"""
        CREATE TABLE weather_daily AS
        SELECT stnId, stnNm, tm AS baseDate, avgTa, minTa, maxTa,
               COALESCE(sumRn, 0) AS rainMm,  -- 일강수량이 빈 날은 거의 모두 일기 현상에도 비 기록이 없다
               sumRn IS NOT NULL AS rainRecorded,
               avgWs, maxWs, avgRhm, sumSsHr, iscs AS weatherNote
        FROM {src('asos_daily')}
        ORDER BY baseDate, stnId
    """)
    con.execute("""
        CREATE VIEW weather_island_daily AS
        SELECT baseDate, count(avgTa) AS stationCnt, avg(avgTa) AS avgTa, min(minTa) AS minTa, max(maxTa) AS maxTa,
               avg(rainMm) AS rainMm, max(rainMm) AS rainMaxMm, avg(avgWs) AS avgWs
        FROM weather_daily GROUP BY baseDate
    """)


def _load_tour_local(con: duckdb.DuckDBPyConnection, src) -> None:
    con.execute(f"""
        CREATE TABLE tour_local_daily AS
        SELECT baseYmd AS baseDate, signguNm AS sigungu,
               regexp_replace(touDivNm, '\\(.*\\)', '') AS visitorClass, touNum AS visitors
        FROM {src('tour_local')}
        ORDER BY baseDate, sigungu, visitorClass
    """)


def _load_bus_station(con: duckdb.DuckDBPyConnection, src) -> None:
    # stationId 는 앞자리 0이 있는 문자열이다. bus_stops 의 ID와 형식이 달라 연결되지 않는다
    raw = f"read_csv('{src.path('bus_daily')}', types = {{'stationId': 'VARCHAR'}})"
    days, first, last = con.execute(f"SELECT count(DISTINCT baseDate), min(baseDate), max(baseDate) FROM {raw}").fetchone()
    end = datetime.strptime(BUS_STATION_END, "%Y-%m-%d").date()
    if last != end or first.day != 1 or days != (end - first).days + 1:
        raise ValueError(
            f"bus_daily.csv 가 어느 달 1일부터 {end} 까지 빠짐없이 있지 않습니다: {first}~{last}, {days}일. fetch_api.py 로 끝까지 받아 주세요"
        )
    # 이름이 전부 빈 날이 있어 같은 ID가 다른 날 가장 많이 쓴 이름으로 채운다
    con.execute(f"""
        CREATE TABLE bus_station_daily AS
        WITH src AS (
            SELECT baseDate, stationId, NULLIF(trim(stationName), '') AS stationName, moveType, priceType, userCount
            FROM {raw}
        ), names AS (
            SELECT stationId, stationName FROM src WHERE stationName IS NOT NULL
            GROUP BY stationId, stationName
            QUALIFY row_number() OVER (PARTITION BY stationId ORDER BY sum(userCount) DESC, stationName) = 1
        ), days AS (
            SELECT s.baseDate, sum(s.userCount) AS total, any_value(d.isOffDay) AS isOffDay
            FROM src s JOIN dim_date d USING (baseDate) GROUP BY s.baseDate
        ), flags AS (
            SELECT baseDate,
                   total < {BUS_STATION_RATIO_LOW} * median(total) OVER (PARTITION BY date_trunc('month', baseDate), isOffDay) AS isPartialDay
            FROM days
        )
        SELECT s.baseDate, s.stationId, COALESCE(s.stationName, n.stationName) AS stationName,
               s.stationName IS NULL AND n.stationName IS NOT NULL AS isNameFilled,
               {NAME_KEY_SQL.format('COALESCE(s.stationName, n.stationName)')} AS nameKey,
               s.moveType, s.priceType, s.userCount AS userCnt, f.isPartialDay
        FROM src s LEFT JOIN names n USING (stationId) JOIN flags f USING (baseDate)
        ORDER BY s.baseDate, s.stationId, s.moveType, s.priceType
    """)


def _load_bus(con: duckdb.DuckDBPyConnection, src) -> None:
    route = src("bus_route_daily")
    con.execute(f"""
        CREATE TABLE bus_route_boarding_daily AS
        SELECT baseDate, busNumber::VARCHAR AS busNumber, sum(userCount) AS boardingCnt
        FROM {route} WHERE actType = '승차' GROUP BY baseDate, busNumber
    """)
    con.execute(f"""
        CREATE TABLE bus_boarding_daily AS
        WITH daily AS (
            SELECT baseDate, sum(boardingCnt) AS boardingCnt, count(*) AS routeCnt
            FROM bus_route_boarding_daily GROUP BY baseDate
        ), rolling AS (
            SELECT *, median(routeCnt) OVER (ORDER BY baseDate ROWS BETWEEN 14 PRECEDING AND 14 FOLLOWING) AS routeMedian
            FROM daily
        )
        SELECT baseDate, boardingCnt, routeCnt, routeMedian, routeCnt / routeMedian AS routeRatio,
               routeCnt / routeMedian < {ROUTE_RATIO_LOW} OR routeCnt / routeMedian > {ROUTE_RATIO_HIGH} AS isAbnormalDay,
               CASE WHEN routeCnt / routeMedian < {ROUTE_RATIO_LOW} THEN '노선 수 적음'
                    WHEN routeCnt / routeMedian > {ROUTE_RATIO_HIGH} THEN '노선 수 많음' END AS abnormalReason
        FROM rolling ORDER BY baseDate
    """)
    con.execute("""
        CREATE OR REPLACE TABLE bus_route_boarding_daily AS
        SELECT r.*, b.isAbnormalDay FROM bus_route_boarding_daily r JOIN bus_boarding_daily b USING (baseDate)
        ORDER BY baseDate, busNumber
    """)
    con.execute(f"""
        CREATE TABLE bus_act_user_daily AS
        SELECT r.baseDate, actType, userType, sum(userCount) AS userCnt, b.isAbnormalDay
        FROM {route} r JOIN bus_boarding_daily b USING (baseDate)
        GROUP BY r.baseDate, actType, userType, b.isAbnormalDay
        ORDER BY r.baseDate, actType, userType
    """)
    con.execute(f"""
        CREATE TABLE bus_hourly AS
        SELECT h.*, b.isAbnormalDay
        FROM {src('bus_hourly')} h LEFT JOIN bus_boarding_daily b USING (baseDate)
    """)
    _load_bus_stop_names(con, src)
    con.execute(f"""
        CREATE TABLE bus_stop_daily AS
        SELECT s.baseDate, m.nameKey, sum(s.userCount) AS userCnt, b.isAbnormalDay
        FROM {src('bus_stop_user_daily')} s
        JOIN bus_stop_name_map m USING (stationName)
        LEFT JOIN bus_boarding_daily b USING (baseDate)
        GROUP BY s.baseDate, m.nameKey, b.isAbnormalDay
        ORDER BY s.baseDate, m.nameKey
    """)
    con.execute("""
        CREATE VIEW bus_weather_daily AS
        SELECT b.baseDate, b.boardingCnt, b.routeCnt, d.year, d.month, d.dayOfWeek, d.isOffDay,
               w.avgTa, w.rainMm, w.rainMaxMm, w.avgWs
        FROM bus_boarding_daily b
        JOIN dim_date d USING (baseDate)
        JOIN weather_island_daily w USING (baseDate)
        WHERE NOT b.isAbnormalDay
    """)


def _load_bus_stop_names(con: duckdb.DuckDBPyConnection, src) -> None:
    strict = NAME_KEY_SQL
    # 이름 뒤의 (종점), (600번) 같은 괄호 설명을 뺀 키. 위 키로 찾지 못한 이름만 쓴다
    loose = strict.format(r"regexp_replace({0}, '\([^)]*\)', '', 'g')")
    con.execute(f"""
        CREATE TABLE bus_stop_name_map AS
        WITH names AS (
            SELECT DISTINCT stationName FROM {src('bus_stop_user_daily')}
        ), stops AS (
            SELECT stationName, {strict.format('stationName')} AS strictKey, {loose.format('stationName')} AS looseKey,
                   longitude, latitude, sigungu
            FROM bus_stops
        ), by_strict AS (
            SELECT strictKey AS k, count(*) AS n, avg(longitude) AS lon, avg(latitude) AS lat,
                   greatest(max(longitude) - min(longitude), max(latitude) - min(latitude)) AS spread,
                   count(DISTINCT sigungu) AS sg, min(sigungu) AS sigungu
            FROM stops GROUP BY strictKey
        ), by_loose AS (
            SELECT looseKey AS k, count(*) AS n, avg(longitude) AS lon, avg(latitude) AS lat,
                   greatest(max(longitude) - min(longitude), max(latitude) - min(latitude)) AS spread,
                   count(DISTINCT sigungu) AS sg, min(sigungu) AS sigungu
            FROM stops GROUP BY looseKey
        ), joined AS (
            SELECT n.stationName, {strict.format('n.stationName')} AS nameKey,
                   CASE WHEN n.stationName IN (SELECT stationName FROM stops) THEN 'exact'
                        WHEN s.k IS NOT NULL THEN 'normalized'
                        WHEN l.k IS NOT NULL THEN 'loose'
                        ELSE 'none' END AS matchType,
                   COALESCE(s.n, l.n, 0) AS stationCnt,
                   COALESCE(s.spread, l.spread) AS spread,
                   COALESCE(s.lon, l.lon) AS lon, COALESCE(s.lat, l.lat) AS lat,
                   COALESCE(s.sg, l.sg) AS sg, COALESCE(s.sigungu, l.sigungu) AS sigungu
            FROM names n
            LEFT JOIN by_strict s ON s.k = {strict.format('n.stationName')}
            LEFT JOIN by_loose l ON l.k = {loose.format('n.stationName')}
        )
        SELECT stationName, nameKey, matchType, stationCnt,
               COALESCE(spread >= {STOP_SPREAD_DEGREE}, FALSE) AS isAmbiguous,
               CASE WHEN spread < {STOP_SPREAD_DEGREE} THEN lon END AS longitude,
               CASE WHEN spread < {STOP_SPREAD_DEGREE} THEN lat END AS latitude,
               CASE WHEN sg = 1 AND spread < {STOP_SPREAD_DEGREE} THEN sigungu END AS sigungu
        FROM joined ORDER BY stationName
    """)


def _clean(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
    return text or None


def _read_center_list(wb: openpyxl.Workbook) -> list[dict]:
    items = []
    category = None
    for row in wb["목록"].iter_rows(min_row=5, values_only=True):
        no, label, title, exportable, provider = row[:5]
        if not title:
            continue
        # 분류 칸의 개수 표기는 실제 개수와 다를 수 있어 (경제 25 → 실제 32) 이름만 쓴다
        category = re.sub(r"\s*\(\d+\)", "", label).strip() if label else category
        items.append(
            {
                "no": _clean(no),
                "category": category,
                "title": _clean(title),
                "exportable": exportable == "가능",
                "provider": _clean(provider),
            }
        )
    return items


def _read_center_sheet(ws, item: dict) -> tuple[dict, list[tuple]]:
    """데이터 시트의 설명과 컬럼 표를 읽는다. 컬럼 표 아래의 [참고] 표는 읽지 않는다."""
    rows = list(ws.iter_rows(values_only=True))
    meta: dict[str, object] = {}
    header_at = None
    for i, row in enumerate(rows):
        first = _clean(row[0])
        if first == "No":
            header_at = i
            break
        for j, cell in enumerate(row[:-1]):
            if _clean(cell) in ("설명", "등록일자", "반출"):
                meta[_clean(cell)] = next((v for v in row[j + 1 :] if v is not None), None)
    if header_at is None:
        raise ValueError(f"{ws.title}: 컬럼 표를 찾지 못했습니다")
    names = [_clean(c) for c in rows[header_at]]
    position = {name: k for k, name in enumerate(names) if name}
    name_col = position.get("컬럼명(영문)", position.get("Column 명"))
    korean_col = position.get("컬럼명(한글)", position.get("한글명"))

    columns = []
    for row in rows[header_at + 1 :]:
        if not re.fullmatch(r"\d+", _clean(row[0]) or ""):
            break
        columns.append(
            (
                item["no"],
                int(_clean(row[0])),
                _clean(row[name_col]),
                _clean(row[korean_col]),
                _clean(row[position["Data Type"]]),
                _clean(row[position["비고"]]),
            )
        )
    if not columns:
        raise ValueError(f"{ws.title}: 컬럼이 없습니다")
    if (meta.get("반출") == "데이터 반출 가능") != item["exportable"]:
        raise ValueError(f"{ws.title}: 목록과 상세의 반출 값이 다릅니다")
    registered = meta.get("등록일자")
    return {
        "description": _clean(meta.get("설명")),
        "registered": registered.date() if isinstance(registered, datetime) else None,
    }, columns


def _load_center_spec(con: duckdb.DuckDBPyConnection) -> None:
    if not CENTER_SPEC.exists():
        raise FileNotFoundError(f"{CENTER_SPEC} 가 없습니다")
    wb = openpyxl.load_workbook(CENTER_SPEC, read_only=True, data_only=True)
    sheets = {re.match(r"\d-\d\d", s).group(): s for s in wb.sheetnames if re.match(r"\d-\d\d", s)}
    datasets, columns = [], []
    for item in _read_center_list(wb):
        if item["no"] == "-":  # 참고 코드표: 설명과 컬럼 표 없이 코드만 있다
            item["no"] = re.match(r"\((참고표\d)\)", item["title"]).group(1)
            header = next(wb[item["title"]].iter_rows(min_row=3, max_row=3, values_only=True))
            names = [_clean(c) for c in header if c is not None]
            columns += [(item["no"], k + 1, n, None, None, None) for k, n in enumerate(names)]
            info = {"description": "분류 코드와 이름을 담은 참고 코드표", "registered": None}
            count = len(names)
        else:
            info, sheet_columns = _read_center_sheet(wb[sheets[item["no"]]], item)
            columns += sheet_columns
            count = len(sheet_columns)
        datasets.append(
            (
                item["no"], item["category"], item["title"], item["exportable"], item["provider"],
                info["registered"], count, info["description"],
            )
        )  # fmt: skip
    wb.close()
    con.execute("""
        CREATE TABLE center_datasets (
            centerNo VARCHAR PRIMARY KEY, category VARCHAR, title VARCHAR, exportable BOOLEAN, provider VARCHAR,
            registeredDate DATE, columnCount INTEGER, description VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE center_columns (
            centerNo VARCHAR, ordinal INTEGER, columnName VARCHAR, columnNameKo VARCHAR, dataType VARCHAR, note VARCHAR,
            PRIMARY KEY (centerNo, ordinal)
        )
    """)
    con.executemany("INSERT INTO center_datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", datasets)
    con.executemany("INSERT INTO center_columns VALUES (?, ?, ?, ?, ?, ?)", columns)
    print(f"  센터 명세: 데이터 {len(datasets)}종, 컬럼 {len(columns)}개")


def _load_catalog(con: duckdb.DuckDBPyConnection) -> None:
    catalog = json.loads((REFERENCE_DIR / "catalog.json").read_text(encoding="utf-8"))
    con.execute("""
        CREATE TABLE data_catalog (
            id VARCHAR PRIMARY KEY, title VARCHAR, status VARCHAR, dataGroup VARCHAR, provider VARCHAR, form VARCHAR,
            license VARCHAR, tableName VARCHAR, dateColumn VARCHAR, startDate DATE, endDate DATE, rowCount BIGINT,
            grain VARCHAR, features VARCHAR, description VARCHAR, questions VARCHAR, limits VARCHAR, keywords VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE data_columns (
            tableName VARCHAR, columnName VARCHAR, columnType VARCHAR, description VARCHAR,
            PRIMARY KEY (tableName, columnName)
        )
    """)
    for d in catalog["datasets"]:
        table, date_col = d["table"], d["dateColumn"]
        start = end = rows = None
        if table:
            # 기간과 행 수는 문서가 낡지 않도록 손으로 적지 않고 실제 표에서 계산한다
            rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if date_col:
                start, end = con.execute(f'SELECT min("{date_col}"), max("{date_col}") FROM "{table}"').fetchone()
        con.execute(
            "INSERT INTO data_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                d["id"], d["title"], d["status"], d["group"], d["provider"], d["form"], d["license"], table, date_col,
                start, end, rows, d["grain"], d["features"], d["description"], d["questions"], d["limits"],
                d["keywords"],
            ],
        )  # fmt: skip

    docs = catalog["columns"]
    actual = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'main'"
    ).fetchall()
    missing = [f"{t}.{c}" for t, c, _ in actual if c not in docs.get(t, {})]
    actual_keys = {(t, c) for t, c, _ in actual}
    stale = [f"{t}.{c}" for t, cols in docs.items() for c in cols if (t, c) not in actual_keys]
    if missing or stale:
        raise ValueError(f"catalog.json 과 DB 컬럼이 다릅니다. 설명 없음: {missing}, 없는 컬럼: {stale}")
    for t, c, kind in actual:
        con.execute("INSERT INTO data_columns VALUES (?, ?, ?, ?)", [t, c, kind, docs[t][c]])

    for d in catalog["datasets"]:
        if d["status"] == "사용" and not d["table"]:
            raise ValueError(f"사용 데이터에 표가 없습니다: {d['id']}")


def _check(con: duckdb.DuckDBPyConnection, src) -> None:
    """집계와 결합에서 값이 사라지거나 늘지 않았는지 원본과 대조한다."""

    def one(sql: str):
        return con.execute(sql).fetchone()[0]

    def same(name: str, built_sql: str, source_sql: str) -> None:
        built, source = one(built_sql), one(source_sql)
        if built != source:
            raise ValueError(f"{name}: DB {built}, 원본 {source}")
        print(f"  일치: {name} = {built:,}")

    route, stop = src("bus_route_daily"), src("bus_stop_user_daily")
    same(
        "관광지 입장객 합계",
        "SELECT sum(visitorCnt) FROM spot_visitors_view",
        f"SELECT sum(visitorCnt) FROM {src('spot_visitors_monthly')}",
    )
    same(
        "버스 승차 합계",
        "SELECT sum(boardingCnt) FROM bus_boarding_daily",
        f"SELECT sum(userCount) FROM {route} WHERE actType = '승차'",
    )
    same(
        "노선별 승차 합계",
        "SELECT sum(boardingCnt) FROM bus_route_boarding_daily",
        f"SELECT sum(userCount) FROM {route} WHERE actType = '승차'",
    )
    same("승하차·유형별 합계", "SELECT sum(userCnt) FROM bus_act_user_daily", f"SELECT sum(userCount) FROM {route}")
    same("정류소 이용 합계", "SELECT sum(userCnt) FROM bus_stop_daily", f"SELECT sum(userCount) FROM {stop}")
    same("시간대별 합계", "SELECT sum(userCount) FROM bus_hourly", f"SELECT sum(userCount) FROM {src('bus_hourly')}")
    same("기상 행 수", "SELECT count(*) FROM weather_daily", f"SELECT count(*) FROM {src('asos_daily')}")
    same("관광지 수", "SELECT count(*) FROM dim_spot", "SELECT count(DISTINCT spotName) FROM spot_visitors_monthly")
    same("기초지자체 방문자 행 수", "SELECT count(*) FROM tour_local_daily", f"SELECT count(*) FROM {src('tour_local')}")
    same(
        "기초지자체 방문자 합계",
        "SELECT round(sum(visitors), 1) FROM tour_local_daily",
        f"SELECT round(sum(touNum), 1) FROM {src('tour_local')}",
    )
    same("정류소별 이용자 합계", "SELECT sum(userCnt) FROM bus_station_daily", f"SELECT sum(userCount) FROM {src('bus_daily')}")
    same("정류소별 이용자 행 수", "SELECT count(*) FROM bus_station_daily", f"SELECT count(*) FROM {src('bus_daily')}")

    same("카드 업종 금액 합계", "SELECT sum(useAmount) FROM card_industry_monthly", f"SELECT sum(useAmount) FROM {src('card_industry_monthly')}")
    same("카드 시간대 금액 합계", "SELECT sum(useAmount) FROM card_time_monthly", f"SELECT sum(useAmountMillion) * 1000000 FROM {src('card_time_monthly')}")
    for table, key in (("card_industry_monthly", "industry, sex"), ("card_time_monthly", "timeSlot, ageRange")):
        if one(f"SELECT count(*) FROM {table} WHERE spenderGroup IS NULL"):
            raise ValueError(f"{table}: 나누지 못한 이용자 유형이 있습니다")
        if one(f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY dtYearMonth, userType, {key} HAVING count(*) > 1)"):
            raise ValueError(f"{table}: 같은 조건의 행이 여럿입니다")
    # 시간대 파일은 백만 원 아래를 버린 것으로 보여 조금 작다. 크게 벗어나면 다른 자료가 된 것이다
    low, high = con.execute("""
        SELECT min(t.v / i.v), max(t.v / i.v) FROM
        (SELECT dtYearMonth, sum(useAmount) AS v FROM card_time_monthly GROUP BY 1) t JOIN
        (SELECT dtYearMonth, sum(useAmount) AS v FROM card_industry_monthly GROUP BY 1) i USING (dtYearMonth)
    """).fetchone()
    if low < CARD_RATIO_LOW or high > CARD_RATIO_HIGH:
        raise ValueError(f"카드 시간대·업종 금액 비율이 벗어났습니다: {low:.3f}~{high:.3f}")
    print(f"  카드 시간대/업종 금액 비율: {low:.3f}~{high:.3f}")

    same("BSI 행 수", "SELECT count(*) FROM sbiz_bsi_monthly", f"SELECT count(*) FROM {src('sbiz_bsi_monthly')}")
    if one("SELECT count(*) FROM (SELECT 1 FROM sbiz_bsi_monthly GROUP BY dtYearMonth, region, outlook HAVING count(*) > 1)"):
        raise ValueError("sbiz_bsi_monthly: 같은 달·지역·구분의 행이 여럿입니다")
    restaurants = src("restaurants")
    window = f"BETWEEN DATE '{RESTAURANT_START}' AND DATE '{RESTAURANT_END}' - 1"
    same("음식점 인허가 건수", "SELECT sum(cnt) FROM restaurant_monthly WHERE eventType = '인허가'", f"SELECT count(*) FROM {restaurants} WHERE licenseDate {window}")
    same("음식점 폐업 건수", "SELECT sum(cnt) FROM restaurant_monthly WHERE eventType = '폐업'", f"SELECT count(*) FROM {restaurants} WHERE closeDate {window}")
    if one("SELECT count(*) FROM restaurants WHERE closeDate < licenseDate OR (status = '폐업') <> (closeDate IS NOT NULL)") > 1:
        raise ValueError("restaurants: 폐업 상태와 폐업일이 맞지 않는 행이 늘었습니다")
    # 입도객: 허브로 만든 2021-08 이후 값과 플랫폼 값이 같아야 이어 붙인 자료로 쓸 수 있다
    overlap, differ = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE h.domesticCnt <> a.domesticCnt OR h.foreignCnt <> a.foreignCnt)
        FROM arrivals_hub_monthly h JOIN arrivals_monthly a USING (dtYearMonth)
    """).fetchone()
    if differ or overlap < one("SELECT count(*) FROM arrivals_hub_monthly"):
        raise ValueError(f"입도객: 허브 값과 플랫폼 값이 다른 달 {differ}개, 겹치는 달 {overlap}개")
    print(f"  입도객: 허브와 겹치는 {overlap}개월 값이 모두 같다")
    same("입도객 달 수", "SELECT count(*) FROM arrivals_monthly", f"SELECT count(*) FROM {src('arrivals_long_monthly')}")
    same("크루즈 승객 합계", "SELECT sum(passengerCnt) FROM cruise_monthly", f"SELECT sum(passengerCnt) FROM {src('cruise_monthly')}")
    same("차량 도착 합계", "SELECT sum(arrivalCnt) FROM vehicle_arrivals_monthly", f"SELECT sum(arrivalCnt) FROM {src('vehicle_arrivals_monthly')}")
    for table in ("vehicle_arrivals_monthly",):
        short = con.execute(f"""
            SELECT string_agg(strftime(m, '%Y-%m') || ' ' || n || '곳', ', ') FROM (
                SELECT dtYearMonth AS m, count(DISTINCT area) AS n FROM {table} GROUP BY 1
                HAVING count(DISTINCT area) < (SELECT count(DISTINCT area) FROM {table}))
        """).fetchone()[0]
        print(f"  {table}: 읍면동이 빠진 달 {short or '없음'}")
    same("1인당 지출 행 수", "SELECT count(*) FROM tourist_spending_monthly", f"SELECT count(*) FROM {src('tourist_spending_monthly')}")
    if one("SELECT count(*) FROM (SELECT 1 FROM tourist_spending_monthly GROUP BY dtYearMonth, item HAVING count(*) > 1)"):
        raise ValueError("tourist_spending_monthly: 같은 달·항목의 행이 여럿입니다")
    same("행사 행 수", "SELECT count(*) FROM jeju_events", f"SELECT count(*) FROM {src('jeju_events')}")
    other = one("SELECT count(*) FROM restaurants WHERE foodGroup = '기타'")
    print(f"  음식점 묶음이 기타인 업소: {other:,}곳")

    uneven = one("""
        SELECT count(*) FROM (
            SELECT dtYearMonth FROM visit_purpose GROUP BY dtYearMonth
            HAVING sum(visitorCnt) FILTER (WHERE classType = '여행형태')
                <> sum(visitorCnt) FILTER (WHERE classType = '방문목적')
        )
    """)
    if uneven:
        raise ValueError(f"여행형태와 방문목적 합계가 다른 달 {uneven}개")
    repeated = one("""
        SELECT count(*) FROM (
            SELECT 1 FROM airport_passengers_monthly GROUP BY dtYearMonth, airport, direction HAVING count(*) > 1
        )
    """)
    if repeated:
        raise ValueError(f"공항 이용자 중복 {repeated}건")

    conflict = one("""
        SELECT count(*) FROM spot_visitors_2024 y JOIN dim_spot d USING (spotName) WHERE y.sigungu <> d.sigungu
    """)
    if conflict or one("SELECT count(*) FROM dim_spot WHERE sigungu IS NULL"):
        raise ValueError(f"dim_spot 시군 값 오류. 허브 값과 다른 관광지 {conflict}곳")
    duplicated = one("SELECT count(*) FROM (SELECT alias FROM spot_alias GROUP BY alias HAVING count(*) > 1)")
    if duplicated:
        raise ValueError(f"한 별칭이 여러 관광지를 가리킵니다: {duplicated}개")

    low, high, total = con.execute("""
        SELECT count(*) FILTER (WHERE abnormalReason = '노선 수 적음'),
               count(*) FILTER (WHERE abnormalReason = '노선 수 많음'), count(*)
        FROM bus_boarding_daily
    """).fetchone()
    print(f"  버스 이상일: {low + high}/{total}일 (노선 수 적음 {low}, 많음 {high})")
    matched, names, share = con.execute("""
        SELECT count(*) FILTER (WHERE matchType <> 'none' AND NOT isAmbiguous), count(*),
               (SELECT sum(d.userCnt) FILTER (WHERE m.nameKey IS NOT NULL) / sum(d.userCnt) FROM bus_stop_daily d
                LEFT JOIN (SELECT DISTINCT nameKey FROM bus_stop_name_map
                           WHERE matchType <> 'none' AND NOT isAmbiguous) m USING (nameKey))
        FROM bus_stop_name_map
    """).fetchone()
    print(f"  정류소 이름 위치 연결: {matched}/{names}개, 이용 인원 기준 {share:.1%}")
    partial, filled_days, filled_share, unnamed = con.execute("""
        SELECT count(DISTINCT baseDate) FILTER (WHERE isPartialDay),
               count(DISTINCT baseDate) FILTER (WHERE isNameFilled),
               sum(userCnt) FILTER (WHERE isNameFilled) / sum(userCnt) FILTER (WHERE isNameFilled OR stationName IS NULL),
               count(DISTINCT stationId) FILTER (WHERE stationName IS NULL)
        FROM bus_station_daily
    """).fetchone()
    print(f"  bus_daily 일부 누락 추정 {partial}일, 이름 채운 날 {filled_days}일 (이용 인원 기준 {filled_share:.2%}), 이름 없는 ID {unnamed}개")


class _Sources:
    """processed CSV 이름을 DuckDB 조회 구문으로 바꾼다."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, name: str) -> str:
        path = self.directory / f"{name}.csv"
        if not path.exists():
            raise FileNotFoundError(f"{path} 가 없습니다. fetch_api.py, prepare_files.py 를 먼저 실행해 주세요")
        return path.as_posix()

    def __call__(self, name: str) -> str:
        return f"read_csv('{self.path(name)}')"


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    src = _Sources(_path("DATA_PROCESSED_DIR", "data/processed"))
    out = _path("DATA_DB_PATH", "data/jeju.duckdb")
    temp = out.with_name(out.name + ".tmp")
    temp.unlink(missing_ok=True)

    con = duckdb.connect(str(temp))
    try:
        con.execute("SET preserve_insertion_order = false")
        steps = (
            ("기본 표", lambda: _load_base_tables(con, src)),
            ("외국인 입도객", lambda: _load_foreign_arrivals(con, src)),
            ("카드 이용", lambda: _load_card(con, src)),
            ("소상공인 경기·음식점·행사·관광객 지출", lambda: _load_local_business(con, src)),
            ("크루즈·렌터카·차량 도착·읍면동 방문객", lambda: _load_platform(con, src)),
            ("관광지 표기·시군", lambda: _load_spots(con)),
            ("날짜·기상", lambda: _load_calendar_and_weather(con, src)),
            ("기초지자체 방문자", lambda: _load_tour_local(con, src)),
            ("정류소별 이용자", lambda: _load_bus_station(con, src)),
            ("버스 집계, 정류소 이름 통일", lambda: _load_bus(con, src)),
            ("센터 명세", lambda: _load_center_spec(con)),
            ("카탈로그", lambda: _load_catalog(con)),
            ("원본 대조", lambda: _check(con, src)),
        )
        for label, step in steps:
            print(f"[{label}]")
            step()
        con.execute("CHECKPOINT")
    except Exception:
        con.close()
        temp.unlink(missing_ok=True)
        raise
    con.close()
    temp.replace(out)
    print(f"저장: {out} ({out.stat().st_size / 1024**2:.0f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
