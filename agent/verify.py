"""답변의 숫자가 도구 결과에 있는지 대조한다. LLM이 숫자를 만들지 않게 하는 마지막 확인이다."""

from __future__ import annotations

import json
import re

from agent.common import ToolResult, clean

_DATE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?|\d{2}-\d{2}")
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|퍼센트|배|명|억|만|천)?")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_YEAR_MONTH_WORD = re.compile(r"\s*(년|월|일|곳|개|위|줄|종|시|박|mm|℃|도)")
_MULTIPLIER = {"천": 1_000, "만": 10_000, "억": 100_000_000}
# 단위 없는 작은 수는 순서·횟수인 경우가 많아 대조하지 않는다
SMALL_NUMBER = 31


def _numbers_in(text: str) -> list[tuple[float, str, int]]:
    """(값, 단위, 소수 자릿수) 목록. 날짜와 연도는 뺀다."""
    found = []
    stripped = _DATE.sub(" ", text)
    for match in _NUMBER.finditer(stripped):
        digits, unit = match.group(1).replace(",", ""), match.group(2) or ""
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        word = _YEAR_MONTH_WORD.match(stripped, match.end(1))
        if word and not unit:
            if word.group(1) == "년" or float(digits) <= SMALL_NUMBER:
                continue
        found.append((float(digits), unit, decimals))
    return found


def _collect(value, into: set[float]) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _collect(v, into)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, into)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (int, float)):
        into.add(abs(float(value)))
    elif isinstance(value, str):
        for number, unit, _ in _numbers_in(value):
            into.add(number * _MULTIPLIER.get(unit, 1))


def allowed_numbers(results: list[ToolResult], question: str = "") -> set[float]:
    allowed: set[float] = set()
    for result in results:
        _collect(clean(result.for_llm()), allowed)
        # 줄여서 보낸 행도 화면에 나가므로 대조에 넣는다
        _collect(clean(result.rows), allowed)
    _collect(question, allowed)
    return allowed


def _matches(value: float, unit: str, decimals: int, allowed: set[float]) -> bool:
    if unit in _MULTIPLIER:
        # "약 93만 명" 처럼 줄인 값은 그 자릿수로 반올림해 같으면 인정한다
        scale = _MULTIPLIER[unit] / 10**decimals
        return any(round(a / scale) * scale == round(value * _MULTIPLIER[unit]) for a in allowed)
    return any(round(a, decimals) == value for a in allowed)


def _allowed_years(results: list[ToolResult], question: str) -> set[str]:
    text = question + " ".join(json.dumps(clean(r.for_llm()), ensure_ascii=False) + json.dumps(clean(r.rows), ensure_ascii=False) for r in results)
    return set(_YEAR.findall(text))


def unverified_numbers(answer: str, results: list[ToolResult], question: str = "") -> list[str]:
    """도구 결과에서 찾지 못한 답변의 숫자와 연도들. "2010~2023년 자료" 같은 지어낸 기간을 잡는다."""
    allowed = allowed_numbers(results, question)
    known_years = _allowed_years(results, question)
    missing = [f"{y}년" for y in _YEAR.findall(answer) if y not in known_years]
    for value, unit, decimals in _numbers_in(answer):
        if not unit and value <= SMALL_NUMBER and decimals == 0:
            continue
        if not _matches(value, unit, decimals, allowed):
            missing.append(f"{value:g}{unit}")
    return list(dict.fromkeys(missing))
