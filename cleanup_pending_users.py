#!/usr/bin/env python3
"""24시간 동안 이메일 인증을 끝내지 않은 가입 대기 Auth 사용자를 정리한다.

GitHub Actions 등 신뢰할 수 있는 서버 환경에서만 실행한다. Service Role 키를
브라우저나 클라이언트 코드에 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


PENDING_NAME = "인증 대기"
MAX_AGE = timedelta(days=1)


def request(url, key, method="GET"):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Supabase HTTP {error.code}: {detail}") from error


def is_expired_pending(user, now):
    metadata = user.get("user_metadata") or {}
    created_at = str(user.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        not user.get("email_confirmed_at")
        and not user.get("phone_confirmed_at")
        and metadata.get("display_name") == PENDING_NAME
        and created <= now - MAX_AGE
    )


def main():
    base = os.environ.get("SUPABASE_URL", "").replace("/rest/v1/", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERV_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_SERVICE_ROLE_KEY 설정이 필요합니다.")

    now, page, deleted = datetime.now(timezone.utc), 1, 0
    while True:
        result = request(f"{base}/auth/v1/admin/users?page={page}&per_page=1000", key) or {}
        users = result.get("users", [])
        for user in users:
            if not is_expired_pending(user, now):
                continue
            user_id = str(user.get("id") or "")
            # profiles.id는 auth.users를 on delete cascade로 참조한다. Auth를 삭제하면
            # 프로필과 세션/refresh token도 함께 정리되어 같은 이메일로 재가입 가능하다.
            request(f"{base}/auth/v1/admin/users/{user_id}", key, "DELETE")
            deleted += 1
            print(f"deleted pending user: {user_id}")
        if len(users) < 1000:
            break
        page += 1
    print(json.dumps({"deleted": deleted, "threshold_hours": 24}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"pending-user cleanup failed: {error}", file=sys.stderr)
        raise
