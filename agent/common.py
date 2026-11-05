"""도구들이 함께 쓰는 DB 연결, 결과 형식, 기간 처리."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# LLM에 보내는 행 수. 화면에는 전체를 보여 주고 숫자 대조도 전체로 한다
LLM_MAX_ROWS = 60
COUNT_KEYS = {"value", "valueA", "valueB", "recentValue", "priorValue", "latestYearValue"}

_con: duckdb.DuckDBPyConnection | None = None


def _db_path() -> Path:
    p = Path(os.getenv("DATA_DB_PATH", "data/jeju.duckdb"))
    return p if p.is_absolute() else PROJECT_ROOT / p


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    """읽기 전용으로 조회한다. 화면에서 여러 스레드가 부를 수 있어 호출마다 커서를 나눈다."""
    global _con
    if _con is None:
        _con = duckdb.connect(str(_db_path()), read_only=True)
    return _con.cursor().execute(sql, params or []).df()


@dataclass
class ToolResult:
    """모든 도구가 돌려주는 형식. status 의 뜻은 README 의 분석 도구 설계에 있다."""

    status: str
    message: str = ""
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    chart: dict | None = None
    evidence: dict = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    # 화면에만 보여 주고 LLM에는 보내지 않는 값. 제3자 제공이 금지된 허브 목록이 여기에 들어간다
    display_only: dict = field(default_factory=dict)

    def __post_init__(self):
        self.rows = _round_counts(self.rows)
        self.summary = _round_counts(self.summary)

    def for_llm(self) -> dict:
        data = {
            "status": self.status,
            "message": self.message,
            "rows": self.rows[:LLM_MAX_ROWS],
            "summary": self.summary,
            "evidence": self.evidence,
            "limits": self.limits,
            "alternatives": self.alternatives,
        }
        if len(self.rows) > LLM_MAX_ROWS:
            data["rowsOmitted"] = len(self.rows) - LLM_MAX_ROWS
        return data

    def to_llm_json(self) -> str:
        return json.dumps(clean(self.for_llm()), ensure_ascii=False)


def _round_counts(value):
    """추정치 지표의 소수를 인원 값에서 없앤다. DB의 round 와 같게 .5 는 올린다."""
    if isinstance(value, dict):
        return {k: (math.floor(float(v) + 0.5) if k in COUNT_KEYS and v is not None and not pd.isna(v) else _round_counts(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_counts(v) for v in value]
    return value


def clean(value):
    """numpy 값과 NaN 을 JSON 으로 보낼 수 있는 값으로 바꾼다. 정수인 실수는 정수로 쓴다."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is pd.NaT or value is pd.NA:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return int(value) if value.is_integer() else value
    if isinstance(value, (date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    return value


def invalid(message: str, **kwargs) -> ToolResult:
    return ToolResult("invalid_request", message, **kwargs)


def pct_change(new: float, base: float) -> float | None:
    return round(100.0 * (new / base - 1), 1) if base else None


def ratio_pct(new: float, base: float) -> float | None:
    return round(100.0 * new / base, 1) if base else None


_MONTH = re.compile(r"(\d{4})(?:[-./]?(\d{1,2}))?(?:[-./]?\d{1,2})?")


def parse_month(text: str, is_end: bool = False) -> pd.Period:
    """'2024' 또는 '2024-05' 를 월로 바꾼다. 연도만 있으면 시작은 1월, 끝은 12월이다."""
    match = _MONTH.fullmatch(str(text).strip())
    month = int(match.group(2)) if match and match.group(2) else (12 if is_end else 1)
    if not match or not 1 <= month <= 12:
        raise ValueError(f"기간은 YYYY 또는 YYYY-MM 형식으로 주세요: {text}")
    return pd.Period(year=int(match.group(1)), month=month, freq="M")


def month_text(period: pd.Period) -> str:
    return period.strftime("%Y-%m")


def month_range_text(start: pd.Period, end: pd.Period) -> str:
    return f"{month_text(start)}~{month_text(end)}"
