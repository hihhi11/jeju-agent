"""센터 명세 v13 구조를 따른 가상 CSV를 만든다. 센터 원본은 개발 단계에서 받을 수 없어 가상 데이터로 개발·시연한다 (대회 게시판 답변).

    python scripts/make_center_sample.py              # data/center_sample/ 에 1-08 두 달, 4-15 한 달
    python scripts/make_center_sample.py --days 3     # 4-15를 3일치만 (빠른 시험용)

값은 무작위이며 실제 제주 현황이 아니다. 파일 이름에 '가상' 을 붙여 화면과 답변에 가상 데이터라고 표시되게 한다.
업종·법정동·국적·행정동 목록은 명세서 참고표에서 읽고, 법정동·행정동의 권역(dstrct_se) 배정은 명세에 없어 이 스크립트가 정한 가상 값이다.
헤더, 인코딩(UTF-8), 결측값(NA)은 센터 CSV 안내(COMPETITION.md 10절)를 따른다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = PROJECT_ROOT / "데이터명세서_kr_v13.xlsx"
OUT_DIR = PROJECT_ROOT / "data" / "center_sample"
CARD_SHEET = "1-08 카드 이용 현황(읍면동)_v02 "
FOREIGN_SHEET = "4-15 단기체류 외국인 유동인구(읍면동)_v02"

# 권역 배정 (가상). 명세에는 7개 권역 이름만 있고 법정동·행정동과의 연결이 없다
REGION_OF = {
    "제주시 동부": "구좌 조천 우도 봉개 회천 용강 도련 삼양 화북",
    "제주시 중부(동)": "일도 이도 건입 아라 영평 오등 월평",
    "제주시 중부(서)": "삼도 용담 연동 노형 오라 도남 해안",
    "제주시 서부": "한림 애월 한경 추자 외도 이호 도두 내도 도평",
    "서귀포시 동부": "성산 표선 남원 하효 신효 보목 토평 상효 효돈",
    "서귀포시 서부": "대정 안덕",
}
SEOGWIPO_DONG = set("강정 남원 대정 대포 도순 동홍 법환 보목 상예 상효 색달 서귀 서호 서홍 성산 신효 안덕 영남 중문 토평 표선 하예 하원 하효 호근 회수".split())
USER_TYPES = ["제주도민", "내국인", "법인", "중국", "일본", "동남아", "기타외국"]
AGES = ["10", "20", "30", "40", "50", "60", "70", "NA"]
SEXES = ["남성", "여성", "알수없음"]


def _side_table(sheet: str, first_row: int, columns: tuple[int, ...]) -> list[tuple]:
    ws = openpyxl.load_workbook(SPEC, read_only=True)[sheet]
    rows = list(ws.iter_rows(values_only=True))[first_row:]
    return [tuple(r[c] if len(r) > c else None for c in columns) for r in rows if len(r) > columns[0] and r[columns[0]]]


def reference_lists() -> dict:
    """명세서 시트 오른쪽 참고표: 업종 42, 법정동 74(월평동 2), 국적 31, 행정동 43."""
    return {
        "industries": _side_table(CARD_SHEET, 28, (5, 6)),
        "dongs": _side_table(CARD_SHEET, 38, (1, 2)),
        "nationalities": _side_table(FOREIGN_SHEET, 21, (1, 2)),
        "emds": _side_table(FOREIGN_SHEET, 21, (5, 6)),
    }


def _region(city: str, name: str, note: str | None = None) -> str:
    if note:
        return note
    for region, words in REGION_OF.items():
        if region.startswith(city) and any(name.startswith(w) for w in words.split()):
            return region
    return "서귀포시 중부" if city == "서귀포시" else "제주시 중부(서)"


def _card_city(name: str, note: str | None) -> str:
    if note:
        return note.split(" ")[0]
    return "서귀포시" if any(name.startswith(w) for w in SEOGWIPO_DONG) else "제주시"


def make_card(month: str, seed: int = 0, combos_per_cell: int = 6) -> pd.DataFrame:
    """1-08 카드 이용 현황(읍면동)_v02 한 달. 법정동×업종마다 이용자 조합 몇 개를 뽑는다."""
    rng = np.random.default_rng(seed)
    refs = reference_lists()
    rows = []
    for name, note in refs["dongs"]:
        city = _card_city(name, note)
        region = _region(city, name, note)
        area_type = "읍면" if name.endswith(("읍", "면")) else "도심"
        dong_scale = rng.lognormal(0, 0.8)
        for code, industry in refs["industries"]:
            if rng.random() < 0.35:
                continue
            for _ in range(combos_per_cell):
                user = rng.choice(USER_TYPES, p=[0.4, 0.3, 0.15, 0.05, 0.03, 0.04, 0.03])
                tour = "도민" if user == "제주도민" else ("NA" if user == "법인" else rng.choice(["개별", "단체"], p=[0.85, 0.15]))
                age = "NA" if user == "법인" else rng.choice(AGES[:-1])
                sex = "알수없음" if user == "법인" else rng.choice(SEXES[:2])
                users = int(rng.poisson(25 * dong_scale)) + 1
                uses = users + int(rng.poisson(users * 0.4))
                amount = int(uses * rng.lognormal(10, 0.6))
                rows.append([month, month[:4], month[4:], region, city, area_type, name, code, industry, user, tour, age, sex, users, uses, amount])
    columns = ["base_year_month", "pt_year", "pt_month", "dstrct_se", "city", "area_type", "bjd", "biz_cd", "biz_nm", "user_type", "tour_type", "age", "sex", "user_cnt", "use_cnt", "use_amt"]
    frame = pd.DataFrame(rows, columns=columns)
    # 같은 조합이 두 번 뽑히면 한 행으로 합친다 (원본은 조합마다 한 행)
    keys = columns[:13]
    return frame.groupby(keys, as_index=False, sort=False)[["user_cnt", "use_cnt", "use_amt"]].sum()


def make_foreign(month: str, days: int | None = None, seed: int = 0) -> pd.DataFrame:
    """4-15 단기체류 외국인 유동인구(읍면동)_v02. 0명인 행은 넣지 않는다."""
    rng = np.random.default_rng(seed)
    refs = reference_lists()
    start = pd.Timestamp(f"{month[:4]}-{month[4:]}-01")
    dates = pd.date_range(start, start + pd.offsets.MonthEnd(0), freq="D")[: days or None]
    nat_share = rng.dirichlet(np.ones(len(refs["nationalities"])) * 0.3)
    hour_shape = np.array([0.5, 0.4, 0.35, 0.3, 0.3, 0.35, 0.5, 0.7, 0.9, 1.0, 1.1, 1.2, 1.2, 1.2, 1.2, 1.2, 1.1, 1.0, 0.9, 0.85, 0.8, 0.7, 0.6, 0.55])
    parts = []
    for code, full in refs["emds"]:
        city, name = full.split(" ", 1)
        region = _region(city, name)
        base = rng.lognormal(3.5, 1.0)
        expected = base * np.outer(hour_shape, nat_share)  # 시각 24 × 국적 31
        for day in dates:
            counts = rng.poisson(expected * rng.uniform(0.8, 1.2))
            hours, nats = np.nonzero(counts)
            if not len(hours):
                continue
            parts.append(pd.DataFrame({
                "base_date": day.strftime("%Y%m%d"),
                "pt_year": day.strftime("%Y"),
                "pt_month": str(day.month),
                "time": [f"{h + 1:02d}" for h in hours],
                "dstrct_se": region,
                "nationality": [refs["nationalities"][n][0] for n in nats],
                "emd_cd": str(code),
                "emd_nm": full,
                "visit_pop_cnt": counts[hours, nats],
            }))
    return pd.concat(parts, ignore_index=True)


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    """센터 CSV처럼 UTF-8, 쉼표, 결측값 NA 로 쓴다."""
    return frame.to_csv(index=False, quoting=csv.QUOTE_NONNUMERIC, na_rep="NA").encode("utf-8")


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="센터 명세 구조의 가상 CSV 만들기")
    parser.add_argument("--days", type=int, help="4-15를 이 날 수만큼만 만든다")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        f"가상_1-08_카드이용현황읍면동_202407.csv": make_card("202407", seed=1),
        f"가상_1-08_카드이용현황읍면동_202408.csv": make_card("202408", seed=2),
        f"가상_4-15_단기체류외국인유동인구_202506.csv": make_foreign("202506", args.days, seed=3),
    }
    for name, frame in outputs.items():
        path = OUT_DIR / name
        path.write_bytes(to_csv_bytes(frame))
        print(f"  {path.relative_to(PROJECT_ROOT)}: {len(frame):,}행, {path.stat().st_size / 1e6:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
