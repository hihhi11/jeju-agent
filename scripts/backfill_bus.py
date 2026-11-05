"""bus_daily 를 2026-09부터 과거로 하루씩 받고, 한 달이 다 차면 DB에 넣는 스크립트.

    python scripts/backfill_bus.py           # 작업 스케줄러가 매일 실행한다 (register_backfill_task.ps1)
    python scripts/backfill_bus.py --status  # 진행 상황 확인

한도에 걸리면 한 시간마다, 서버 오류면 10분마다 다시 시도하고 그날 23:30에 끝낸다.
DB는 새 파일로 만들어 원본·시험 문제 대조를 통과해야 바꾼다. 실패하면 CSV와 DB를 그대로 둔다.
기록은 data/backfill_bus.log 에 남는다.
"""

from __future__ import annotations

import argparse
import msvcrt
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

import fetch_api
from fetch_api import ApiError, PROJECT_ROOT, SOURCES, _fetch_chunk, _path, _save_raw, _ymd

LATEST = date(2026, 9, 30)
OLDEST = date(2026, 1, 1)
DB_END = "20261031"  # build_db.py 의 BUS_STATION_END 와 같은 날
UNTIL = date(2026, 11, 11)  # 11-12부터 검증과 기능 동결이라 그 전날까지만 받는다
STOP_HOUR, STOP_MINUTE = 23, 30
QUOTA_WAIT = 3600
ERROR_WAIT = 600
NO_WINDOW = subprocess.CREATE_NO_WINDOW


class DataMismatch(Exception):
    """받은 행이 API 총 건수·날짜와 맞지 않음. 재시도로 풀리지 않을 수 있어 그날 실행을 멈춘다."""


def _log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def _log_path() -> Path:
    return _path("DATA_RAW_DIR", "data/raw").parent / "backfill_bus.log"


def _raw_file(day: date) -> Path:
    return _path("DATA_RAW_DIR", "data/raw") / "api" / "bus_daily" / f"{_ymd(day)}_{_ymd(day)}.json"


def _missing_days() -> list[date]:
    days = [LATEST - timedelta(days=i) for i in range((LATEST - OLDEST).days + 1)]
    return [d for d in days if not _raw_file(d).exists()]


def _complete_start() -> date | None:
    """LATEST 에서 과거로 이어서 다 받은 달 중 가장 이른 달의 1일."""
    start = None
    last = LATEST
    while last >= OLDEST:
        first = last.replace(day=1)
        if any(not _raw_file(first + timedelta(days=i)).exists() for i in range((last - first).days + 1)):
            break
        start = first
        last = first - timedelta(days=1)
    return start


def _db_start(db: Path) -> date | None:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("SELECT min(baseDate) FROM bus_station_daily").fetchone()[0]
    finally:
        con.close()


def _fetch_day(day: date) -> list[dict]:
    s = SOURCES["bus_daily"]
    params = s.params(day, day)
    _, _, total = fetch_api._page_hub(s, params, 1, 1)
    rows = _fetch_chunk(s, params)
    if len(rows) != int(total):
        raise DataMismatch(f"{day}: 받은 행 {len(rows)}개, API 총 건수 {total}개")
    if {row["baseDate"] for row in rows} - {_ymd(day)}:
        raise DataMismatch(f"{day}: 다른 날짜의 행이 섞여 있습니다")
    return rows


def _run(script: str, env: dict) -> bool:
    # pythonw 에서는 자식의 표준 출력이 없을 수 있어 python.exe 를 창 없이 실행한다
    python = Path(sys.executable).with_name("python.exe")
    done = subprocess.run(
        [str(python), str(PROJECT_ROOT / "scripts" / script)],
        env=env,
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    print(done.stdout + done.stderr, end="", flush=True)
    return done.returncode == 0


def publish() -> bool:
    """다 받은 달까지 CSV와 DB를 늘린다. 대조를 통과하지 못하면 원래대로 둔다."""
    start = _complete_start()
    db = _path("DATA_DB_PATH", "data/jeju.duckdb")
    try:
        if start is None or start == _db_start(db):
            return True
    except duckdb.Error as e:
        _log(f"DB를 읽지 못해 반영을 미룹니다: {e}")
        return False

    _log(f"DB 반영 시작: {start} ~ {DB_END}")
    csv = _path("DATA_PROCESSED_DIR", "data/processed") / "bus_daily.csv"
    csv_backup = csv.with_suffix(".csv.prev")
    staged = db.with_name(db.stem + ".next" + db.suffix)
    shutil.copy2(csv, csv_backup)
    published = False
    try:
        fetch_api.collect("bus_daily", _ymd(start), DB_END, False)
        env = {**os.environ, "DATA_DB_PATH": str(staged), "PYTHONIOENCODING": "utf-8"}
        if _run("build_db.py", env) and _run("check_eval_questions.py", env):
            shutil.copy2(db, db.with_name(db.stem + ".prev" + db.suffix))
            staged.replace(db)
            published = True
            _log(f"DB 반영 완료: {start} ~ {DB_END}")
        else:
            _log("대조에 실패해 CSV와 DB를 그대로 둡니다")
    except (OSError, ApiError, ValueError) as e:
        # 개발 중인 프로그램이 DB를 열고 있으면 바꾸지 못하고 다음 실행에서 다시 시도한다
        _log(f"DB 반영 실패, 다음에 다시 시도합니다: {e}")
    finally:
        if not published:
            shutil.copy2(csv_backup, csv)
        csv_backup.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
    return published


def _deadline(now: datetime) -> datetime:
    stop = now.replace(hour=STOP_HOUR, minute=STOP_MINUTE, second=0, microsecond=0)
    return stop if now < stop else stop + timedelta(days=1)


def _wait(seconds: int, deadline: datetime) -> None:
    time.sleep(max(0, min(seconds, (deadline - datetime.now()).total_seconds())))


def collect_days(deadline: datetime) -> None:
    quota_blocked = False
    for day in _missing_days():
        while True:
            if datetime.now() >= deadline:
                _log("오늘 실행 시간이 끝났습니다")
                return
            try:
                rows = _fetch_day(day)
                break
            except ApiError as e:
                _log(f"{day} 실패: {e}")
                quota_blocked = quota_blocked or "quota" in str(e).lower()
                _wait(QUOTA_WAIT if "quota" in str(e).lower() else ERROR_WAIT, deadline)
        if quota_blocked:
            _log("호출 한도가 풀린 것을 확인했습니다")
            quota_blocked = False
        _save_raw(_raw_file(day), rows)
        _log(f"{day} 저장: {len(rows)}행")
        if day.day == 1:
            publish()
    _log(f"{OLDEST} 까지 모두 받았습니다")


def show_status() -> int:
    missing = _missing_days()
    total = (LATEST - OLDEST).days + 1
    print(f"받은 날: {total - len(missing)}/{total}일 ({OLDEST} ~ {LATEST})")
    print(f"다음에 받을 날: {missing[0] if missing else '없음'}")
    print(f"다 받은 달의 시작: {_complete_start() or '없음'}")
    print(f"DB 기간 시작: {_db_start(_path('DATA_DB_PATH', 'data/jeju.duckdb'))}")
    log = _log_path()
    if log.exists():
        print("최근 기록:")
        print("\n".join(log.read_text(encoding="utf-8").splitlines()[-8:]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="bus_daily 과거 방향 수집")
    parser.add_argument("--status", action="store_true", help="진행 상황 출력")
    parser.add_argument("--until", default=_ymd(UNTIL), help="이 날까지만 받기 YYYYMMDD. 지나면 DB 반영만 한다")
    args = parser.parse_args()
    if args.status:
        sys.stdout.reconfigure(errors="replace")
        return show_status()

    # 작업 스케줄러는 창 없는 pythonw 로 실행하므로 모든 출력을 파일로 보낸다
    sys.stdout = sys.stderr = open(_log_path(), "a", encoding="utf-8", buffering=1)
    # 프로세스가 끝나면 운영체제가 푸는 잠금이라 비정상 종료해도 남지 않는다
    lock = open(_log_path().with_suffix(".lock"), "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _log("이미 실행 중이라 끝냅니다")
        return 0

    now = datetime.now()
    _log(f"시작 (남은 날: {len(_missing_days())}일)")
    publish()
    if now.date() > datetime.strptime(args.until, "%Y%m%d").date():
        _log(f"수집 기한 {args.until} 이 지나 받지 않습니다")
        return 0
    try:
        collect_days(_deadline(now))
    except DataMismatch as e:
        _log(f"행 수 확인 실패로 오늘 수집을 멈춥니다. 확인이 필요합니다: {e}")
    publish()
    _log("끝")
    return 0


if __name__ == "__main__":
    sys.exit(main())
