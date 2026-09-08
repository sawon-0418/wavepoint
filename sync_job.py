#!/usr/bin/env python3
"""Render Cron Job용 공식 낚시터·보호구역 동기화 실행기.

웹 서버와 분리되어 한 번만 동기화한 뒤 종료한다. 실패 시 0이 아닌 종료
코드를 반환해 Render의 Cron Job 실행 기록에서 실패를 확인할 수 있다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from server import sync_all


def main() -> int:
    started_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    print(f"[{started_at}] 공식 데이터 동기화를 시작합니다.", flush=True)
    result = sync_all()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result["errors"]:
        print("동기화 중 오류가 발생했습니다.", file=sys.stderr, flush=True)
        return 1
    print("공식 데이터 동기화가 완료됐습니다.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
