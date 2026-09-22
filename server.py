#!/usr/bin/env python3
"""물결포인트 로컬/API 서버 — 키는 .env에서만 읽습니다."""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import re
import threading
import time
import math
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.cookies import SimpleCookie
from html import escape as html_escape
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_EVENTS: dict[str, list[float]] = {}

def consume_rate_limit(key, maximum, window_seconds):
    """메모리 기반 계정별 요청 제한. 단일 서버 배포의 스팸 방어용이다."""
    now = time.time()
    with RATE_LIMIT_LOCK:
        events = [event for event in RATE_LIMIT_EVENTS.get(key, []) if event > now - window_seconds]
        if len(events) >= maximum:
            RATE_LIMIT_EVENTS[key] = events
            return False, max(1, int(window_seconds - (now - events[0])))
        events.append(now)
        RATE_LIMIT_EVENTS[key] = events
    return True, 0

def load_env():
    path = ROOT / ".env"
    if not path.exists(): return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))

load_env()

def supabase_value(*names):
    return next((os.environ.get(name, "") for name in names if os.environ.get(name, "")), "")

def supabase_base_url():
    """프로젝트 URL 또는 실수로 입력한 REST URL 모두 안전하게 정규화한다."""
    return re.sub(r"/rest/v1/?$", "", supabase_value("SUPABASE_URL").rstrip("/"))

def supabase_request(path, method="GET", body=None, prefer="return=representation"):
    """Supabase Service Role 키는 이 서버 안에서만 사용한다."""
    url = supabase_base_url()
    key = supabase_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERV_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase URL 또는 Service Role Key 설정이 없습니다.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method, headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": prefer})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Supabase HTTP {error.code}: {detail}") from error

def supabase_auth(path, method="GET", body=None, access_token=None):
    """Supabase Auth는 서버를 통해서만 호출한다. Service Role은 브라우저에 전달하지 않는다."""
    url = supabase_base_url()
    key = supabase_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERV_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase 인증 설정이 없습니다.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"apikey": key, "Authorization": f"Bearer {access_token or key}", "Content-Type": "application/json"}
    request = urllib.request.Request(f"{url}/auth/v1/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        try:
            message = json.loads(detail).get("msg") or json.loads(detail).get("message")
            detail = message or detail
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RuntimeError(f"인증 요청 실패: {detail}") from error

def confirmed_member_count():
    """운영자 전용 표시용: 이메일 또는 전화 인증까지 끝난 Auth 사용자만 센다."""
    page, count = 1, 0
    while True:
        result = supabase_auth(f"admin/users?page={page}&per_page=1000") or {}
        users = result.get("users", [])
        count += sum(bool(user.get("email_confirmed_at") or user.get("phone_confirmed_at")) for user in users)
        if len(users) < 1000:
            return count
        page += 1

def user_profile(user):
    user_id = str((user or {}).get("id") or "")
    if not user_id: return None
    rows = supabase_request(f"profiles?select=id,display_name,role&id=eq.{urllib.parse.quote(user_id, safe='')}") or []
    profile = rows[0] if rows else {}
    metadata = user.get("user_metadata") or {}
    email = str(user.get("email") or "").strip().lower()
    # 권한은 profiles의 과거 값만 신뢰하지 않고 운영자 허용 목록과도 매 요청마다 대조한다.
    # 가입 전후 순서와 무관하게 admin_emails에 등록된 이메일은 즉시 운영자 권한을 얻는다.
    try:
        admin_rows = supabase_request(f"admin_emails?select=email&email=eq.{urllib.parse.quote(email, safe='')}") or [] if email else []
    except RuntimeError:
        # 배포·초기 설정 단계에서 운영자 목록 테이블이 아직 없더라도
        # 로그인 세션 전체가 실패하지 않게 기존 프로필 권한으로 동작한다.
        admin_rows = []
    role = "admin" if admin_rows else (profile.get("role") or "user")
    return {"id": user_id, "email": email, "displayName": profile.get("display_name") or metadata.get("display_name") or "낚시꾼", "role": role}

def attach_member_labels(*collections):
    """운영자 목록에 닉네임과 짧은 회원 식별자를 붙인다. 이메일은 보내지 않는다."""
    rows = [item for collection in collections for item in collection]
    user_ids = []
    for item in rows:
        try:
            user_ids.append(str(uuid.UUID(str(item.get("user_id") or ""))))
        except (ValueError, AttributeError):
            continue
    profile_names = {}
    if user_ids:
        profiles = supabase_request(f"profiles?select=id,display_name,role&id=in.({','.join(dict.fromkeys(user_ids))})") or []
        profile_names = {str(profile.get("id")): profile for profile in profiles}
    for item in rows:
        user_id = str(item.get("user_id") or "")
        profile = profile_names.get(user_id, {})
        item["reporter_name"] = profile.get("display_name") or ("기존 익명 항목" if not user_id else "회원")
        item["reporter_role"] = profile.get("role") or "user"
        item["reporter_key"] = user_id[:8] if user_id else ""
    return rows

def storage_request(url, key, name, binary, mime_type):
    request = urllib.request.Request(
        f"{url}/storage/v1/object/post-images/{name}", data=binary, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": mime_type, "x-upsert": "false"},
    )
    with urllib.request.urlopen(request, timeout=90):
        pass

def ensure_post_image_bucket(url, key):
    """초기 SQL이 아직 적용되지 않은 배포에서도 사진 버킷을 한 번 자동 준비한다."""
    payload = json.dumps({"id": "post-images", "name": "post-images", "public": True, "file_size_limit": 50 * 1024 * 1024, "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"]}).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/storage/v1/bucket", data=payload, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except urllib.error.HTTPError as error:
        # 이미 존재하는 버킷은 그대로 사용한다. 다른 오류는 원인을 사용자에게 전달한다.
        if error.code not in (400, 409):
            detail = error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"사진 저장소를 준비하지 못했습니다. (HTTP {error.code}: {detail})") from error

def delete_post_image(image_url):
    if not image_url: return
    marker = "/storage/v1/object/public/post-images/"
    if marker not in image_url: return
    name = image_url.split(marker, 1)[1]
    if not name or "/" not in name: return
    url = supabase_base_url()
    key = supabase_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERV_ROLE_KEY")
    request = urllib.request.Request(
        f"{url}/storage/v1/object/post-images/{urllib.parse.quote(name, safe='/')}", method="DELETE",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError(f"게시글 사진을 삭제하지 못했습니다. (HTTP {error.code})") from error

def delete_account_content(user):
    """회원탈퇴 전에 Auth 사용자에 연결된 서비스 데이터를 정리한다."""
    user_id = str(user["id"])
    encoded_user_id = urllib.parse.quote(user_id, safe="")
    owned_posts = supabase_request(f"posts?author_id=eq.{encoded_user_id}&select=id,image_url") or []
    owned_spots = supabase_request(f"community_spots?user_id=eq.{encoded_user_id}&select=id") or []
    post_ids = {str(post.get("id")) for post in owned_posts if post.get("id")}
    spot_ids = {str(spot.get("id")) for spot in owned_spots if spot.get("id")}

    # 사용자가 공유한 포인트에 연결된 조과는 포인트 삭제 뒤 고아 데이터가 되므로 함께 삭제한다.
    for spot_id in spot_ids:
        linked_posts = supabase_request(f"posts?spot_id=eq.{urllib.parse.quote(spot_id, safe='')}&select=id,image_url") or []
        owned_posts.extend(linked_posts)
        post_ids.update(str(post.get("id")) for post in linked_posts if post.get("id"))
    for post in {str(post.get("id")): post for post in owned_posts if post.get("id")}.values():
        delete_post_image(post.get("image_url"))

    # 대상이 사라지는 신고와 탈퇴자가 제출한 신고·문의는 개인 데이터이므로 함께 제거한다.
    for post_id in post_ids:
        encoded_post_id = urllib.parse.quote(post_id, safe="")
        supabase_request(f"reports?kind=eq.post&target_id=eq.{encoded_post_id}", "DELETE", None, "return=minimal")
        supabase_request(f"posts?id=eq.{encoded_post_id}", "DELETE", None, "return=minimal")
    for spot_id in spot_ids:
        encoded_spot_id = urllib.parse.quote(spot_id, safe="")
        supabase_request(f"reports?kind=eq.spot&target_id=eq.{encoded_spot_id}", "DELETE", None, "return=minimal")
        supabase_request(f"community_spots?id=eq.{encoded_spot_id}", "DELETE", None, "return=minimal")

    supabase_request(f"spot_recommendations?user_id=eq.{encoded_user_id}", "DELETE", None, "return=minimal")
    supabase_request(f"post_recommendations?user_id=eq.{encoded_user_id}", "DELETE", None, "return=minimal")
    supabase_request(f"reports?user_id=eq.{encoded_user_id}", "DELETE", None, "return=minimal")
    supabase_request(f"inquiries?user_id=eq.{encoded_user_id}", "DELETE", None, "return=minimal")

    # 운영자 계정도 탈퇴할 수 있도록 과거 검토·숨김 기록의 작성자 참조는 비운다.
    supabase_request(f"reports?reviewed_by=eq.{encoded_user_id}", "PATCH", {"reviewed_by": None})
    supabase_request(f"inquiries?reviewed_by=eq.{encoded_user_id}", "PATCH", {"reviewed_by": None})
    supabase_request(f"posts?hidden_by=eq.{encoded_user_id}", "PATCH", {"hidden_by": None})
    supabase_request(f"community_spots?hidden_by=eq.{encoded_user_id}", "PATCH", {"hidden_by": None})
    supabase_request(f"admin_audit_logs?actor_id=eq.{encoded_user_id}", "DELETE", None, "return=minimal")

    # 탈퇴한 운영자 이메일은 허용 목록에서도 제거해 재가입 시 권한이 자동 복구되지 않게 한다.
    email = urllib.parse.quote(str(user.get("email") or "").strip().lower(), safe="")
    if email:
        supabase_request(f"admin_emails?email=eq.{email}", "DELETE", None, "return=minimal")
    supabase_auth(f"admin/users/{encoded_user_id}", "DELETE")
    return {"posts": len(post_ids), "spots": len(spot_ids)}

def upload_post_image(data_url):
    match = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", data_url or "")
    if not match: raise RuntimeError("JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.")
    binary = base64.b64decode(match.group(2), validate=True)
    if len(binary) > 50 * 1024 * 1024: raise RuntimeError("사진은 50MB 이하만 업로드할 수 있습니다.")
    url = supabase_base_url()
    key = supabase_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERV_ROLE_KEY")
    suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[match.group(1)]
    name = f"posts/{uuid.uuid4()}.{suffix}"
    try:
        storage_request(url, key, name, binary, match.group(1))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        missing_bucket = error.code == 404 or "bucket" in detail.lower() and "not found" in detail.lower()
        if not missing_bucket:
            raise RuntimeError(f"사진 업로드에 실패했습니다. (HTTP {error.code}: {detail})") from error
        ensure_post_image_bucket(url, key)
        try:
            storage_request(url, key, name, binary, match.group(1))
        except urllib.error.HTTPError as retry_error:
            detail = retry_error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"사진 업로드에 실패했습니다. (HTTP {retry_error.code}: {detail})") from retry_error
    return f"{url}/storage/v1/object/public/post-images/{name}"

def catch_fields(payload):
    species = payload.get("species") or ""
    if not isinstance(species, str) or len(species.strip()) > 30:
        raise ValueError("어종은 30자 이내로 입력해 주세요.")
    species = species.strip()
    length = payload.get("length")
    if length is not None and length != "":
        try:
            if isinstance(length, bool): raise ValueError
            length = float(length)
            if not math.isfinite(length) or not 1 <= length <= 300: raise ValueError
        except (TypeError, ValueError):
            raise ValueError("물고기 길이는 1~300cm 사이의 숫자로 입력해 주세요.")
        if not species: raise ValueError("조과 길이를 등록하려면 어종을 입력해 주세요.")
    else:
        length = None
    return {"species": species, "length": length}

def read_json(name, fallback):
    path = DATA / name
    if not path.exists(): return fallback
    try: return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError: return fallback

def write_json(name, value):
    (DATA / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def persist_official_spots(items):
    unique = {str(item.get("id")): item for item in items if item.get("id")}
    records = [{key: item.get(key) for key in ("id", "title", "kind", "lat", "lng", "species", "description", "address", "source")} for item in unique.values()]
    if records: supabase_request("official_spots?on_conflict=id", "POST", records, "resolution=merge-duplicates,return=minimal")

def persist_protected_areas(features):
    unique = {}
    for index, feature in enumerate(features):
        record = {"id": f"{feature.get('properties', {}).get('code') or 'area'}-{index}", "name": feature.get("properties", {}).get("name", "수산자원보호구역"), "code": feature.get("properties", {}).get("code", ""), "geometry": feature.get("geometry")}
        unique[record["id"]] = record
    records = list(unique.values())
    if records: supabase_request("protected_areas?on_conflict=id", "POST", records, "resolution=merge-duplicates,return=minimal")

def migrate_cached_official_data():
    spots = read_json("official_spots.json", {"items": []}).get("items", [])
    areas = read_json("protected_areas.geojson", {"features": []}).get("features", [])
    persist_official_spots(spots); persist_protected_areas(areas)
    return {"spots": len(spots), "areas": len(areas)}

def fetch_json(url, key_name, parameters=None):
    key = os.environ.get(key_name, "")
    if not url: raise ValueError(f"{key_name}에 대응하는 API URL이 설정되지 않았습니다.")
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    for name, value in (parameters or {}).items():
        if value is not None and not query.get(name): query[name] = str(value)
    # 공공데이터포털 Swagger 예시 URL에는 serviceKey= 빈 값이 포함될 수 있다.
    # 비어 있는 예시값은 .env의 실제 키로 반드시 교체한다.
    # 공공데이터포털의 Encoding 키(%2F, %3D 포함)를 urlencode에 그대로 넣으면
    # %가 다시 인코딩된다. 먼저 원문으로 되돌린 뒤 요청을 조립한다.
    if key and not query.get("serviceKey"): query["serviceKey"] = urllib.parse.unquote(key)
    request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment))
    try:
        with urllib.request.urlopen(request_url, timeout=25) as response:
            raw = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        # 공공데이터포털은 인증·활용승인 실패를 HTTP 200 + XML로 반환하는 경우가 있다.
        # 원문 전체나 인증키는 노출하지 않고, 제공기관의 오류 코드/메시지만 보여준다.
        if raw.lstrip().startswith("<"):
            try:
                root = ET.fromstring(raw)
                root_name = root.tag.rsplit("}", 1)[-1]
                if key_name == "PROTECTED_AREAS_API_KEY" and root_name == "FeatureCollection":
                    return {"_gml": raw}
                values = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in root.iter()}
                detail = values.get("errMsg") or values.get("returnAuthMsg") or values.get("resultMsg") or values.get("message")
                code = values.get("errCd") or values.get("returnReasonCode") or values.get("resultCode")
                if key_name == "PROTECTED_AREAS_API_KEY":
                    # 보호구역 원문은 공개 공간정보이므로 변환 개발을 위해 로컬에만 보관한다.
                    (DATA / "protected_areas_last_response.xml").write_text(raw, encoding="utf-8")
                tags = ", ".join(dict.fromkeys(node.tag.rsplit("}", 1)[-1] for node in root.iter()))[:180]
                message = " ".join(value for value in (code, detail) if value) or f"XML 구조: {tags}"
                raise RuntimeError(f"{key_name}가 JSON 대신 XML을 반환했습니다: {message}") from error
            except ET.ParseError:
                pass
        sample = " ".join(raw.split())[:120] or "빈 응답"
        raise RuntimeError(f"{key_name}가 JSON이 아닌 응답을 반환했습니다: {sample}") from error

def local_name(element):
    return element.tag.rsplit("}", 1)[-1]

def epsg5179_to_wgs84(x, y):
    """대한민국 국가좌표계(EPSG:5179)를 지도 표시용 WGS84 경위도로 변환한다."""
    a = 6378137.0
    inverse_flattening = 298.257222101
    flattening = 1 / inverse_flattening
    e2 = flattening * (2 - flattening)
    ep2 = e2 / (1 - e2)
    k0, x0, y0 = 0.9996, 1000000.0, 2000000.0
    lat0, lon0 = math.radians(38.0), math.radians(127.0)
    def meridian_arc(latitude):
        return a * ((1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * latitude
                    - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * latitude)
                    + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * latitude)
                    - (35 * e2**3 / 3072) * math.sin(6 * latitude))
    m = meridian_arc(lat0) + (y - y0) / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu))
    sin_phi, cos_phi, tan_phi = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = a / math.sqrt(1 - e2 * sin_phi**2)
    r1 = a * (1 - e2) / (1 - e2 * sin_phi**2) ** 1.5
    t1, c1 = tan_phi**2, ep2 * cos_phi**2
    d = (x - x0) / (n1 * k0)
    lat = phi1 - (n1 * tan_phi / r1) * (d**2 / 2 - (5 + 3*t1 + 10*c1 - 4*c1**2 - 9*ep2) * d**4 / 24 + (61 + 90*t1 + 298*c1 + 45*t1**2 - 252*ep2 - 3*c1**2) * d**6 / 720)
    lng = lon0 + (d - (1 + 2*t1 + c1) * d**3 / 6 + (5 - 2*c1 + 28*t1 - 3*c1**2 + 8*ep2 + 24*t1**2) * d**5 / 120) / cos_phi
    return [math.degrees(lng), math.degrees(lat)]

def protected_features_from_gml(raw):
    """수산자원보호구역 WFS의 GML 다각형을 GeoJSON으로 변환한다."""
    root = ET.fromstring(raw)
    features = []
    for member in root.iter():
        if local_name(member) != "opn_fshrsrc_pzn_a":
            continue
        name = next((node.text for node in member.iter() if local_name(node) == "fshrsrc_pzn_nm" and node.text), "수산자원보호구역")
        code = next((node.text for node in member.iter() if local_name(node) == "fshrsrc_pzn_cd" and node.text), "")
        polygons = []
        for polygon in (node for node in member.iter() if local_name(node) == "Polygon"):
            ring = []
            for pos_list in (node for node in polygon.iter() if local_name(node) == "posList"):
                values = [float(value) for value in (pos_list.text or "").split()]
                coordinates = [epsg5179_to_wgs84(values[index], values[index + 1]) for index in range(0, len(values) - 1, 2)]
                if len(coordinates) >= 4:
                    ring.append(coordinates)
            if ring:
                polygons.append(ring)
        if polygons:
            geometry = {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else {"type": "MultiPolygon", "coordinates": polygons}
            features.append({"type": "Feature", "properties": {"name": name, "code": code}, "geometry": geometry})
    return features

def first(row, names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""): return value
    return None

def rows_from(payload):
    if isinstance(payload, list): return payload
    if isinstance(payload, dict):
        for path in (("data",), ("rows",), ("response", "body", "items", "item"), ("items", "item")):
            current = payload
            for key in path:
                if isinstance(current, dict): current = current.get(key)
                else: current = None; break
            if isinstance(current, list): return current
    return []

def reverse_geocode(lat, lng):
    """좌표만 제공된 공식 포인트를 사람이 읽을 수 있는 주소로 보완한다."""
    key_id = os.environ.get("NAVER_MAPS_NCP_KEY_ID", "")
    secret = os.environ.get("NAVER_MAPS_CLIENT_SECRET", "")
    if not key_id or not secret:
        return None, "네이버 지도 Key ID 또는 Client Secret이 .env에 없습니다."
    query = urllib.parse.urlencode({"coords": f"{lng},{lat}", "sourcecrs": "epsg:4326", "orders": "roadaddr,addr", "output": "json"})
    request = urllib.request.Request(
        f"https://maps.apigw.ntruss.com/map-reversegeocode/v2/gc?{query}",
        headers={"X-NCP-APIGW-API-KEY-ID": key_id, "X-NCP-APIGW-API-KEY": secret},
    )
    try:
        # 인증/네트워크 문제가 생겼을 때 150개 좌표를 차례로 오래 기다리지 않도록 짧게 제한한다.
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not results:
            return None, "네이버 역지오코딩 결과가 없습니다."
        result = results[0]
        region = result.get("region", {})
        names = [region.get(f"area{index}", {}).get("name", "") for index in range(1, 5)]
        land = result.get("land", {})
        numbers = "-".join(value for value in (land.get("number1", ""), land.get("number2", "")) if value)
        return " ".join(value for value in [*names, land.get("name", ""), numbers] if value), None
    except urllib.error.HTTPError as error:
        # 인증키 자체는 절대 응답에 포함하지 않고, 네이버가 제공한 오류 코드/메시지만 돌려준다.
        detail = error.read().decode("utf-8", errors="replace")[:500]
        try:
            body = json.loads(detail)
            error_info = body.get("error", body) if isinstance(body, dict) else {}
            code = error_info.get("errorCode") or error_info.get("code") or ""
            message = error_info.get("message") or error_info.get("errorMessage") or ""
            detail = " ".join(value for value in (str(code), str(message)) if value).strip()
        except json.JSONDecodeError:
            pass
        return None, f"네이버 역지오코딩 HTTP {error.code}{': ' + detail if detail else ''}"
    except urllib.error.URLError as error:
        return None, f"네이버 역지오코딩 네트워크 오류: {error.reason}"
    except json.JSONDecodeError:
        return None, "네이버 역지오코딩 응답을 해석하지 못했습니다."

def geocode(query):
    """사용자가 직접 입력한 주소·지역을 지도 이동 후보로 조회한다."""
    key_id = os.environ.get("NAVER_MAPS_NCP_KEY_ID", "")
    secret = os.environ.get("NAVER_MAPS_CLIENT_SECRET", "")
    if not key_id or not secret:
        return [], "네이버 지도 Key ID 또는 Client Secret이 .env에 없습니다."
    request = urllib.request.Request(
        f"https://maps.apigw.ntruss.com/map-geocode/v2/geocode?{urllib.parse.urlencode({'query': query})}",
        headers={"X-NCP-APIGW-API-KEY-ID": key_id, "X-NCP-APIGW-API-KEY": secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = []
        for value in payload.get("addresses", [])[:8]:
            if not value.get("y") or not value.get("x"): continue
            address = value.get("roadAddress") or value.get("jibunAddress") or query
            items.append({"name": address, "address": address, "lat": float(value["y"]), "lng": float(value["x"])})
        return items, None
    except urllib.error.HTTPError as error:
        return [], f"네이버 장소 검색 오류: {error.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return [], "장소 검색을 완료하지 못했습니다."

def enrich_missing_addresses(points, max_lookups=None):
    """주소 보완 결과를 캐시하고, 사용자 검토용 메모를 함께 생성한다."""
    cache = read_json("reverse_geocode_cache.json", {})
    lookups = 0
    errors = []
    for point in points:
        if point.get("address") != "공식 API 등록 포인트":
            continue
        key = f"{float(point['lat']):.5f},{float(point['lng']):.5f}"
        cached = cache.get(key) or None
        if cached is None:
            if max_lookups is not None and lookups >= max_lookups:
                continue
            address, error = reverse_geocode(point["lat"], point["lng"])
            if address:
                cache[key] = address
            else:
                cache.pop(key, None)  # 일시적인 네트워크 실패는 캐시하지 않아 다음 동기화에서 재시도한다.
                if error and error not in errors:
                    errors.append(error)
                # 인증 또는 네트워크 오류는 다음 좌표도 같은 결과이므로 즉시 중단한다.
                if error and ("HTTP" in error or "네트워크 오류" in error):
                    break
            lookups += 1
            time.sleep(0.12)  # 네이버 역지오코딩 요청을 짧게 분산한다.
        else:
            address = cached or None
        if address:
            point["address"] = address
            point["description"] = address
            point["addressSource"] = "coordinate-reverse-geocoding"
    write_json("reverse_geocode_cache.json", cache)
    memo_lines = ["# 좌표 기반 주소 보완 메모", "", f"생성 시각: {datetime.now().isoformat(timespec='seconds')}", "", "| 낚시터 | 좌표 | 보완 주소 | 상태 |", "| --- | --- | --- | --- |"]
    for point in points:
        if point.get("address") != "공식 API 등록 포인트" and point.get("addressSource") != "coordinate-reverse-geocoding":
            continue
        key = f"{float(point['lat']):.5f},{float(point['lng']):.5f}"
        status = "보완 완료" if point.get("addressSource") == "coordinate-reverse-geocoding" else "조회 대기 또는 주소 확인 불가"
        memo_lines.append(f"| {point['title']} | {key} | {point.get('address', '')} | {status} |")
    (ROOT / "주소보완_메모.md").write_text("\n".join(memo_lines) + "\n", encoding="utf-8")
    return errors

def sync_spots():
    points = []
    page = 1
    while page <= 500:
        payload = fetch_json(os.environ.get("FISHING_SPOTS_API_URL", ""), "FISHING_SPOTS_API_KEY", {"pageNo": page, "numOfRows": 100, "returnType": "JSON"})
        rows = rows_from(payload)
        for row in rows:
            if not isinstance(row, dict): continue
            lat = first(row, ["latitude", "lat", "LTTD", "WGS84_LAT", "위도", "latitde"])
            lng = first(row, ["longitude", "lng", "LNGTD", "WGS84_LOT", "경도", "longtude"])
            try: lat, lng = float(lat), float(lng)
            except (TypeError, ValueError): continue
            name = first(row, ["name", "fishingPlaceName", "FSHNGSPT_NM", "낚시터명", "낚시터명칭", "시설명"]) or "이름 없는 낚시터"
            address = first(row, ["address", "LCTN_ROAD_NM_ADDR", "도로명주소", "소재지도로명주소", "소재지지번주소", "주소"]) or "공식 API 등록 포인트"
            points.append({"id": f"official-{name}-{lat:.5f}-{lng:.5f}", "title": str(name), "kind": "river", "lat": lat, "lng": lng, "species": "공식 낚시터", "description": str(address), "address": str(address), "source": "낚시터 정보 조회서비스"})
        if len(rows) < 100: break
        page += 1
    enrich_missing_addresses(points)
    write_json("official_spots.json", {"updatedAt": datetime.now(timezone.utc).isoformat(), "items": points})
    persist_official_spots(points)
    return len(points)

def sync_protected_areas():
    payload = fetch_json(os.environ.get("PROTECTED_AREAS_WFS_URL", ""), "PROTECTED_AREAS_API_KEY", {"bbox": os.environ.get("PROTECTED_AREAS_BBOX", "700000,1300000,1400000,2200000"), "maxFeatures": 100})
    features = protected_features_from_gml(payload["_gml"]) if isinstance(payload, dict) and payload.get("_gml") else payload.get("features", []) if isinstance(payload, dict) else []
    normalized = {"type": "FeatureCollection", "updatedAt": datetime.now(timezone.utc).isoformat(), "features": features}
    write_json("protected_areas.geojson", normalized)
    persist_protected_areas(features)
    return len(features)

def sync_all():
    result = {"spots": None, "areas": None, "errors": []}
    for label, action in (("spots", sync_spots), ("areas", sync_protected_areas)):
        try: result[label] = action()
        except Exception as error: result["errors"].append(f"{label}: {error}")
    return result

def write_address_enrichment_memo(items):
    rows = ["# 좌표 기반 주소 보완 메모", "", f"생성 시각: {datetime.now().isoformat(timespec='seconds')}", "", "| 낚시터 | 좌표 | 주소 | 상태 |", "| --- | --- | --- | --- |"]
    for point in items:
        if point.get("address") != "공식 API 등록 포인트" and point.get("addressSource") != "naver-js-reverse-geocoding":
            continue
        status = "네이버 좌표→주소 보완 완료" if point.get("addressSource") == "naver-js-reverse-geocoding" else "조회 대기"
        rows.append(f"| {point['title']} | {float(point['lat']):.5f},{float(point['lng']):.5f} | {point.get('address', '')} | {status} |")
    (ROOT / "주소보완_메모.md").write_text("\n".join(rows) + "\n", encoding="utf-8")

def scheduled_sync():
    """매일 정해진 한국 시간에 공식 데이터를 한 번만 갱신한다."""
    hour = max(0, min(23, int(os.environ.get("DAILY_SYNC_HOUR", "3"))))
    minute = max(0, min(59, int(os.environ.get("DAILY_SYNC_MINUTE", "30"))))
    kst = ZoneInfo("Asia/Seoul")
    while True:
        now = datetime.now(kst)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        print(f"다음 공식 데이터 동기화: {next_run.isoformat(timespec='minutes')}")
        threading.Event().wait((next_run - now).total_seconds())
        result = sync_all()
        print(f"[{datetime.now(kst).isoformat(timespec='seconds')}] sync: {result}")

# 공개 SEO 페이지는 JavaScript 실행을 기다리지 않고 서버가 완성된 HTML로 반환한다.
# 지도 본문과 별개로 공개 상태의 공식·사용자 포인트만 사용하며, 운영 데이터는 포함하지 않는다.
SEO_REGIONS = {
    "seoul": ("서울", ("서울",)), "busan": ("부산", ("부산",)),
    "daegu": ("대구", ("대구",)), "incheon": ("인천", ("인천",)),
    "gwangju": ("광주", ("광주",)), "daejeon": ("대전", ("대전",)),
    "ulsan": ("울산", ("울산",)), "sejong": ("세종", ("세종",)),
    "gyeonggi": ("경기도", ("경기",)), "gangwon": ("강원", ("강원",)),
    "north-chungcheong": ("충청북도", ("충북", "충청북도")),
    "south-chungcheong": ("충청남도", ("충남", "충청남도")),
    "north-jeolla": ("전북", ("전북", "전라북도", "전북특별자치도")),
    "south-jeolla": ("전남", ("전남", "전라남도")),
    "north-gyeongsang": ("경북", ("경북", "경상북도")),
    "south-gyeongsang": ("경남", ("경남", "경상남도")),
    "jeju": ("제주", ("제주",)), "geoje": ("거제", ("거제",)),
    "gangneung": ("강릉", ("강릉",)),
}
def public_site_url():
    return os.environ.get("PUBLIC_APP_URL", "https://wavepoint-aghq.onrender.com").strip().rstrip("/")

def seo_slug(value):
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:72] or "spot"

def seo_short_id(value):
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:10]

def seo_spot_url(spot):
    return f"{public_site_url()}/spot/{seo_slug(spot.get('title'))}-{seo_short_id(spot.get('id'))}"

def seo_lastmod(value):
    if not value: return None
    try: return str(value)[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", str(value)) else None
    except (TypeError, ValueError): return None

def seo_public_spots():
    """검색에 공개해도 되는 포인트만 반환한다. DB가 일시적으로 실패하면 공식 캐시만 쓴다."""
    try:
        official = supabase_request("official_spots?select=id,title,kind,lat,lng,species,description,address,source,updated_at&order=title.asc&limit=2000") or []
    except RuntimeError:
        official = read_json("official_spots.json", {"items": []}).get("items", [])
    try:
        community = supabase_request("community_spots?is_hidden=is.false&select=id,title,kind,lat,lng,species,description,address,created_at&order=created_at.desc&limit=1000") or []
    except RuntimeError:
        community = []
    records, seen = [], set()
    for source_name, bucket in (("official", official), ("community", community)):
        for raw in bucket:
            spot_id = str(raw.get("id") or "")
            if not spot_id or spot_id in seen or not raw.get("title"): continue
            seen.add(spot_id)
            item = dict(raw)
            item["id"] = spot_id
            item["kind"] = "sea" if item.get("kind") == "sea" else "river"
            item["updated_at"] = item.get("updated_at") or item.get("created_at")
            item["is_official"] = source_name == "official"
            records.append(item)
    return records

def seo_region_of(spot):
    source = f"{spot.get('address') or ''} {spot.get('description') or ''}"
    for slug, (label, matchers) in SEO_REGIONS.items():
        if any(matcher in source or matcher in str(spot.get("title") or "") for matcher in matchers):
            return slug, label
    return None, None

def seo_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

def product_updates():
    """배포 버전과 함께 관리되는 공개 업데이트 기록을 최신순으로 반환한다."""
    records = []
    for raw in read_json("updates.json", []):
        if not isinstance(raw, dict): continue
        date = str(raw.get("date") or "").strip()
        title = str(raw.get("title") or "").strip()
        items = [str(item).strip() for item in raw.get("items", []) if str(item).strip()]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not title or not items: continue
        try: datetime.strptime(date, "%Y-%m-%d")
        except ValueError: continue
        records.append({"date": date, "title": title, "items": items})
    return sorted(records, key=lambda item: item["date"], reverse=True)

def korean_update_date(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return f"{parsed.year}년 {parsed.month}월 {parsed.day}일"
    except (TypeError, ValueError):
        return str(value or "")

def update_items_rotator(items):
    """JS가 없으면 목록 전체를, 있으면 한 항목씩 순환해 보여 주는 마크업이다."""
    rows = "".join(f'<li class="update-rotator-item">{html_escape(item)}</li>' for item in items)
    return f'<ul class="update-items-rotator" data-update-rotator>{rows}</ul>'

def update_items_list(items):
    """전체 업데이트 기록에서는 변경 항목을 숨김 없이 모두 보여 준다."""
    rows = "".join(f"<li>{html_escape(item)}</li>" for item in items)
    return f'<ul class="update-items-list">{rows}</ul>'

def seo_document(title, description, canonical_path, body, schemas, robots="index,follow"):
    site = public_site_url()
    canonical = f"{site}{canonical_path}"
    json_ld = "\n".join(f'<script type="application/ld+json">{seo_json(schema)}</script>' for schema in schemas)
    google_verification = html_escape(os.environ.get("GOOGLE_SITE_VERIFICATION", ""), quote=True)
    naver_verification = html_escape(os.environ.get("NAVER_SITE_VERIFICATION", ""), quote=True)
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="google-site-verification" content="{google_verification}"><meta name="naver-site-verification" content="{naver_verification}"><title>{html_escape(title)}</title><meta name="description" content="{html_escape(description)}"><meta name="robots" content="{robots}"><meta name="googlebot" content="{robots}"><meta name="theme-color" content="#008b87"><link rel="canonical" href="{html_escape(canonical)}"><link rel="icon" type="image/png" sizes="512x512" href="{site}/favicon.png"><link rel="apple-touch-icon" href="{site}/favicon.png">
<meta property="og:locale" content="ko_KR"><meta property="og:type" content="website"><meta property="og:site_name" content="물결포인트"><meta property="og:title" content="{html_escape(title)}"><meta property="og:description" content="{html_escape(description)}"><meta property="og:url" content="{html_escape(canonical)}"><meta property="og:image" content="{site}/og-image-v2.png"><meta property="og:image:type" content="image/png"><meta property="og:image:width" content="1734"><meta property="og:image:height" content="907"><meta property="og:image:alt" content="낚싯대와 바다 물결로 표현한 물결포인트 로고"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{html_escape(title)}"><meta name="twitter:description" content="{html_escape(description)}"><meta name="twitter:image" content="{site}/og-image-v2.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700;800&display=swap" rel="stylesheet"><link rel="stylesheet" href="/seo-pages.css"><link rel="stylesheet" href="/updates.css?v=20260921-static-history">{json_ld}</head>
<body><header class="seo-header"><a class="seo-brand" href="/"><span>≋</span> 물결포인트</a><nav aria-label="주요 메뉴"><a href="/">낚시 포인트 찾기</a><a href="/posts">게시글</a><a href="/guides">안전 수칙</a></nav></header><main class="seo-main">{body}</main><footer class="seo-footer"><a href="/">낚시 포인트 지도</a><a href="/posts">낚시 게시글</a><a href="/guides/safe-fishing-basics">낚시 안전수칙</a><a href="/updates">업데이트 기록</a><a href="/sitemap.xml">사이트맵</a><span>출조 전 현지 규정과 안전 안내를 최신 기준으로 확인하세요.</span></footer><script src="/updates.js?v=20260921-update-rotator"></script></body></html>'''

def seo_breadcrumb(items):
    site = public_site_url()
    schema = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": position, "name": name, "item": f"{site}{path}"}
        for position, (name, path) in enumerate(items, 1)
    ]}
    links = " <span aria-hidden=\"true\">/</span> ".join(f'<a href="{html_escape(path)}">{html_escape(name)}</a>' for name, path in items)
    return f'<nav class="breadcrumbs" aria-label="현재 위치">{links}</nav>', schema

def seo_spot_card(spot):
    label = "바다낚시" if spot.get("kind") == "sea" else "민물낚시"
    address = spot.get("address") or "주소 정보 미제공"
    species = spot.get("species") or "어종 정보 미제공"
    return f'<article class="spot-card"><p>{label} · {html_escape(species)}</p><h2>{html_escape(str(spot.get("title")))}</h2><span>{html_escape(str(address))}</span><a href="{html_escape(urllib.parse.urlsplit(seo_spot_url(spot)).path)}">{html_escape(str(spot.get("title")))} 상세 정보 보기</a></article>'

def seo_image_url(value):
    """공개 스토리지의 HTTP(S) 이미지 URL만 게시글 목록에 사용한다."""
    url = str(value or "").strip()
    return url if re.match(r"^https?://", url, re.IGNORECASE) else ""

def seo_listing_page(path, title, description, heading, spots, intro, crumbs):
    breadcrumb_html, breadcrumb_schema = seo_breadcrumb(crumbs)
    item_list = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": index, "name": str(spot.get("title")), "url": seo_spot_url(spot)}
        for index, spot in enumerate(spots[:100], 1)
    ]}
    collection = {"@context": "https://schema.org", "@type": "CollectionPage", "name": heading, "url": f"{public_site_url()}{path}", "description": description}
    cards = "".join(seo_spot_card(spot) for spot in spots[:100]) or '<p class="empty-state">아직 공개된 낚시터 정보가 없습니다.</p>'
    body = f'{breadcrumb_html}<header class="page-heading"><p>FISHING SPOTS</p><h1>{html_escape(heading)}</h1><p>{html_escape(intro)}</p><strong>공개 낚시터 {len(spots)}곳</strong></header><section class="spot-grid" aria-label="낚시터 목록">{cards}</section>'
    return seo_document(title, description, path, body, [breadcrumb_schema, collection, item_list])

def seo_detail_page(path, spot):
    kind = "바다낚시" if spot.get("kind") == "sea" else "민물낚시"
    name = str(spot.get("title"))
    address = str(spot.get("address") or "주소 정보 미제공")
    description = f"{name} 낚시 포인트의 {kind} 유형, 대상 어종과 이용자 조과 정보를 확인하세요."
    crumbs = [("홈", "/"), ("낚시 포인트", "/spots"), (name, path)]
    breadcrumb_html, breadcrumb_schema = seo_breadcrumb(crumbs)
    place = {"@context": "https://schema.org", "@type": "Place", "name": name, "url": f"{public_site_url()}{path}", "description": str(spot.get("description") or description)}
    if address != "주소 정보 미제공": place["address"] = address
    # 사용자 공유 포인트는 생태·안전상 정확한 좌표를 구조화 데이터에 싣지 않는다.
    if spot.get("is_official") and spot.get("lat") is not None and spot.get("lng") is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": spot["lat"], "longitude": spot["lng"]}
    body = f'''{breadcrumb_html}<article class="spot-detail-page"><header class="page-heading"><p>{kind} · 낚시 포인트</p><h1>{html_escape(name)}</h1><p>{html_escape(str(spot.get("description") or "등록된 낚시터 정보입니다."))}</p></header><dl class="spot-facts"><div><dt>유형</dt><dd>{kind}</dd></div><div><dt>주소·행정구역</dt><dd>{html_escape(address)}</dd></div><div><dt>주요 어종</dt><dd>{html_escape(str(spot.get("species") or "어종 정보 미제공"))}</dd></div><div><dt>정보 제공</dt><dd>{"공식 낚시터 정보" if spot.get("is_official") else "사용자 공유 포인트"}</dd></div><div><dt>최종 수정일</dt><dd>{html_escape(seo_lastmod(spot.get("updated_at")) or "등록일 정보 미제공")}</dd></div></dl><section><h2>낚시 전 확인하세요</h2><p>금어기, 금지 체장, 보호구역, 출입 통제와 현장 안전 안내는 변동될 수 있습니다. 출조 전 공식 안내와 현장 표지판을 확인해 주세요.</p></section><p><a class="primary-link" href="/?spot={urllib.parse.quote(str(spot.get("id")), safe="")}">물결포인트 지도에서 위치 보기</a></p></article>'''
    return seo_document(f"{name} 낚시 포인트｜어종·조과 정보 – 물결포인트", description, path, body, [breadcrumb_schema, place])

def seo_catch_page(posts, spots):
    path = "/posts"
    title = "낚시 게시글｜조과와 낚시 후기 – 물결포인트"
    description = "물결포인트 이용자가 공개한 낚시 게시글, 조과와 낚시 후기를 최신순으로 확인하세요."
    breadcrumbs, breadcrumb_schema = seo_breadcrumb([("홈", "/"), ("낚시 게시글", path)])
    spots_by_id = {str(spot.get("id")): spot for spot in spots}
    cards = []
    for post in posts[:100]:
        spot = spots_by_id.get(str(post.get("spot_id")))
        species = str(post.get("species") or "낚시 게시글")
        try: length = f" · {float(post.get('length')):.1f}cm" if post.get("length") else ""
        except (TypeError, ValueError): length = ""
        image_url = seo_image_url(post.get("image_url"))
        photo = f'<img class="catch-thumbnail" src="{html_escape(image_url, quote=True)}" alt="{html_escape(species)} 낚시 게시글 사진" width="144" height="144" loading="lazy">' if image_url else ""
        spot_link = f'<a class="catch-spot-link" href="{html_escape(urllib.parse.urlsplit(seo_spot_url(spot)).path)}">{html_escape(str(spot.get("title")))} 포인트 보기</a>' if spot else '<span class="catch-spot-missing">연결된 포인트 정보가 없습니다.</span>'
        cards.append(f'<article class="catch-card{" has-image" if photo else ""}"><div class="catch-card-body"><h2>{html_escape(species)}{length}</h2><p>{html_escape(str(post.get("content") or ""))}</p><span>{html_escape(str(post.get("author") or "낚시꾼"))} · 등록일 {html_escape(seo_lastmod(post.get("created_at")) or "정보 미제공")}</span>{spot_link}</div>{photo}</article>')
    cards_html = "".join(cards) or '<p class="empty-state">아직 공개된 게시글이 없습니다.</p>'
    collection = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "낚시 게시글", "url": f"{public_site_url()}{path}", "description": description}
    body = f'{breadcrumbs}<header class="page-heading"><p>FISHING POSTS</p><h1>낚시 게시글</h1><p>이용자가 공개한 조과와 낚시 후기를 최신순으로 확인합니다.</p><a class="primary-link" href="/?action=rank-catch">포인트를 선택해 조과 등록하기</a></header><section class="catch-grid">{cards_html}</section>'
    return seo_document(title, description, path, body, [breadcrumb_schema, collection])

def seo_guides_page(path):
    title = "낚시 가이드｜안전수칙과 출조 전 확인사항 – 물결포인트"
    description = "안전한 낚시를 위해 금어기, 금지 체장, 기상과 출입 통제를 확인하는 방법을 안내합니다."
    crumbs, crumb_schema = seo_breadcrumb([("홈", "/"), ("낚시 가이드", path)])
    body = f'{crumbs}<header class="page-heading"><p>FISHING GUIDE</p><h1>낚시 가이드</h1><p>출조 전 확인해야 할 안전수칙과 현지 규정 안내입니다.</p></header><section class="guide-list"><article><h2>안전한 낚시를 위한 기본 확인사항</h2><p>구명조끼, 기상과 파도, 출입 통제, 야간 안전, 쓰레기 회수와 현지 규정을 확인하세요.</p><a href="/guides/safe-fishing-basics">낚시 안전수칙 자세히 보기</a></article></section>'
    return seo_document(title, description, path, body, [crumb_schema, {"@context":"https://schema.org","@type":"CollectionPage","name":"물결포인트 낚시 가이드","url":f"{public_site_url()}{path}"}])

def seo_guide_article(path):
    title = "안전한 낚시를 위한 출조 전 확인사항 – 물결포인트 낚시 가이드"
    description = "구명조끼, 기상과 파도, 출입 통제, 금어기와 금지 체장을 출조 전 확인하세요."
    crumbs, crumb_schema = seo_breadcrumb([("홈", "/"), ("낚시 가이드", "/guides"), ("안전수칙", path)])
    body = f'{crumbs}<article class="guide-article"><header class="page-heading"><p>SAFETY GUIDE</p><h1>안전한 낚시를 위한 출조 전 확인사항</h1><p>{html_escape(description)}</p></header><h2>현장 안전</h2><ul><li>구명조끼를 착용하고 기상·파도 예보를 확인하세요.</li><li>출입 통제, 사유지, 항만과 보호구역 안내를 준수하세요.</li><li>야간 낚시에는 조명과 동행 여부를 점검하고 쓰레기를 되가져오세요.</li></ul><h2>낚시 규정</h2><p>금어기와 금지 체장은 어종·해역·지자체 고시에 따라 달라질 수 있습니다. 물결포인트 정보는 참고용이며, 출조 전 최신 공식 기준과 현장 표지판을 확인해야 합니다.</p></article>'
    article = {"@context":"https://schema.org","@type":"Article","headline":"안전한 낚시를 위한 출조 전 확인사항","description":description,"inLanguage":"ko-KR","mainEntityOfPage":f"{public_site_url()}{path}","publisher":{"@type":"Organization","name":"물결포인트","url":public_site_url()}}
    return seo_document(title, description, path, body, [crumb_schema, article])

def seo_updates_page(path="/updates"):
    updates = product_updates()
    crumbs, crumb_schema = seo_breadcrumb([("홈", "/"), ("업데이트 기록", path)])
    cards = []
    for update in updates:
        items = update_items_list(update["items"])
        cards.append(f'<article class="update-card"><time datetime="{html_escape(update["date"], quote=True)}">{html_escape(korean_update_date(update["date"]))}</time><h2>{html_escape(update["title"])}</h2>{items}</article>')
    cards_html = "".join(cards) or '<p class="empty-state">아직 등록된 업데이트 기록이 없습니다.</p>'
    description = "물결포인트의 기능 개선, 오류 수정과 서비스 업데이트 기록을 확인하세요."
    body = f'{crumbs}<header class="page-heading"><p>WEEKLY UPDATE</p><h1>업데이트 기록</h1><p>매주 반영한 기능 개선과 오류 수정 내용을 투명하게 안내합니다.</p></header><section class="update-list" aria-label="업데이트 내역">{cards_html}</section>'
    collection = {"@context":"https://schema.org","@type":"CollectionPage","name":"물결포인트 업데이트 기록","url":f"{public_site_url()}{path}","description":description}
    return seo_document("업데이트 기록 – 물결포인트", description, path, body, [crumb_schema, collection])

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # 정적 파일과 API 응답 모두에 적용되는 기본 브라우저 보안 정책이다.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        static_path = urllib.parse.urlsplit(self.path).path
        if static_path.endswith((".css", ".js", ".png", ".svg", ".webp", ".woff2")):
            self.send_header("Cache-Control", "public, max-age=604800")
        super().end_headers()

    def cookie_suffix(self, max_age):
        force_secure = os.environ.get("FORCE_SECURE_COOKIES", "").lower() in ("1", "true", "yes")
        secure = "; Secure" if force_secure or self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def queue_cookie(self, name, value, max_age):
        if not hasattr(self, "_queued_cookies"): self._queued_cookies = []
        self._queued_cookies.append(f"{name}={value}{self.cookie_suffix(max_age)}")

    def queue_session_cookies(self, payload):
        access_token, refresh_token = (payload or {}).get("access_token", ""), (payload or {}).get("refresh_token", "")
        if not access_token or not refresh_token: return False
        self.queue_cookie("wave_session", access_token, 604800)
        self.queue_cookie("wave_refresh", refresh_token, 604800)
        return True

    def clear_session_cookies(self):
        self.queue_cookie("wave_session", "", 0)
        self.queue_cookie("wave_refresh", "", 0)

    def send_json(self, data, status=200, headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items(): self.send_header(name, value)
        for cookie in getattr(self, "_queued_cookies", []): self.send_header("Set-Cookie", cookie)
        self._queued_cookies = []
        self.end_headers(); self.wfile.write(raw)

    def session_user(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("wave_session").value if cookie.get("wave_session") else ""
            if not token: return None
            return user_profile(supabase_auth("user", access_token=token))
        except (RuntimeError, KeyError, AttributeError):
            # Access token은 짧게 만료될 수 있으므로 HttpOnly refresh token으로 한 번 갱신한다.
            try:
                refresh_token = cookie.get("wave_refresh").value if cookie.get("wave_refresh") else ""
                if not refresh_token: return None
                refreshed = supabase_auth("token?grant_type=refresh_token", "POST", {"refresh_token": refresh_token})
                if not self.queue_session_cookies(refreshed):
                    self.clear_session_cookies(); return None
                return user_profile((refreshed or {}).get("user") or supabase_auth("user", access_token=refreshed["access_token"]))
            except (RuntimeError, KeyError, AttributeError):
                self.clear_session_cookies()
                return None

    def pending_signup_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie.get("wave_signup_verification").value if cookie.get("wave_signup_verification") else ""

    def account_verification_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie.get("wave_account_verification").value if cookie.get("wave_account_verification") else ""

    def password_reset_redirect_url(self):
        """Supabase recovery 메일이 돌아올 공개 주소를 만든다.

        배포에서는 PUBLIC_APP_URL을 고정해 두고, 로컬 개발만 현재 요청 주소를
        사용한다. 링크에는 브라우저에서만 읽는 일회성 recovery 토큰이 붙는다.
        """
        configured = os.environ.get("PUBLIC_APP_URL", "").strip().rstrip("/")
        if configured:
            return f"{configured}/?reset-password=1"
        host = self.headers.get("Host", "127.0.0.1:4173").strip()
        proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() or "http"
        return f"{proto}://{host}/?reset-password=1"

    def require_user(self):
        user = self.session_user()
        if not user:
            self.send_json({"error": "로그인 후 이용할 수 있습니다."}, 401)
            return None
        return user

    def require_admin(self):
        user = self.require_user()
        if not user: return None
        if user.get("role") != "admin":
            self.send_json({"error": "운영자 권한이 필요합니다."}, 403)
            return None
        return user

    def set_session(self, payload):
        if not self.queue_session_cookies(payload):
            user = (payload or {}).get("user") or {}
            return self.send_json({"requiresEmailConfirmation": True, "email": user.get("email", "")}, 202)

        return self.send_json({"user": user_profile((payload or {}).get("user") or {})})

    def require_rate_limit(self, user, action, maximum, window_seconds):
        allowed, retry_after = consume_rate_limit(f"{user['id']}:{action}", maximum, window_seconds)
        if allowed: return True
        self.send_json({"error": f"요청이 너무 많습니다. 약 {retry_after}초 후 다시 시도해 주세요."}, 429, {"Retry-After": str(retry_after)})
        return False

    def send_html(self, html, status=200, robots=None):
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store" if robots and "noindex" in robots else "public, max-age=300")
        if robots: self.send_header("X-Robots-Tag", robots)
        self.end_headers()
        self.wfile.write(raw)

    def send_xml(self, xml):
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(xml.encode("utf-8"))

    def send_seo_not_found(self):
        body = '<main class="seo-main"><h1>페이지를 찾을 수 없습니다</h1><p>요청한 낚시 포인트 또는 가이드는 없거나 더 이상 공개되지 않습니다.</p><p><a href="/spots">낚시 포인트 목록으로 돌아가기</a></p></main>'
        self.send_html(seo_document("페이지를 찾을 수 없습니다 – 물결포인트", "요청한 페이지를 찾을 수 없습니다.", self.path.split("?", 1)[0], body, [], "noindex,nofollow"), 404, "noindex,nofollow")

    def seo_posts(self):
        try:
            posts = supabase_request("posts?is_hidden=is.false&select=id,spot_id,author,content,species,length,image_url,created_at&order=created_at.desc&limit=200") or []
            hidden = supabase_request("community_spots?is_hidden=is.true&select=id") or []
            hidden_ids = {str(item.get("id")) for item in hidden}
            return [post for post in posts if str(post.get("spot_id")) not in hidden_ids]
        except RuntimeError:
            return []

    def serve_main_page(self):
        content = (ROOT / "index.html").read_text(encoding="utf-8")
        updates = product_updates()
        latest = updates[0] if updates else {"date": "", "title": "새로운 업데이트를 준비하고 있습니다.", "items": ["새로운 소식은 업데이트 기록에서 확인할 수 있습니다."]}
        replacements = {
            "{{SITE_URL}}": public_site_url(), "{{CANONICAL_URL}}": f"{public_site_url()}/",
            "{{GOOGLE_SITE_VERIFICATION}}": os.environ.get("GOOGLE_SITE_VERIFICATION", ""),
            "{{NAVER_SITE_VERIFICATION}}": os.environ.get("NAVER_SITE_VERIFICATION", ""),
            "{{LATEST_UPDATE_ISO}}": latest["date"],
            "{{LATEST_UPDATE_DATE}}": korean_update_date(latest["date"]),
            "{{LATEST_UPDATE_TITLE}}": latest["title"],
        }
        for marker, value in replacements.items(): content = content.replace(marker, html_escape(value, quote=True))
        content = content.replace("{{LATEST_UPDATE_ITEMS}}", update_items_rotator(latest["items"]))
        self.send_html(content)

    def serve_sitemap(self):
        site, records = public_site_url(), seo_public_spots()
        posts = self.seo_posts()
        updates = product_updates()
        urls = [("/", None), ("/spots", None), ("/spots/type/sea", None), ("/spots/type/freshwater", None), ("/guides", None), ("/guides/safe-fishing-basics", None), ("/updates", updates[0]["date"] if updates else None)]
        if posts: urls.append(("/posts", max((seo_lastmod(post.get("created_at")) or "" for post in posts), default=None)))
        for slug, (_, matchers) in SEO_REGIONS.items():
            regional = [spot for spot in records if any(matcher in f"{spot.get('title') or ''} {spot.get('address') or ''} {spot.get('description') or ''}" for matcher in matchers)]
            if regional: urls.append((f"/spots/{slug}", max((seo_lastmod(spot.get("updated_at")) or "" for spot in regional), default=None)))
        urls.extend((urllib.parse.urlsplit(seo_spot_url(spot)).path, seo_lastmod(spot.get("updated_at"))) for spot in records)
        # 동일한 URL은 한 번만 내보낸다. 업데이트 시각이 있는 쪽을 우선한다.
        unique = {}
        for path, lastmod in urls:
            if path not in unique or (lastmod and (not unique[path] or lastmod > unique[path])):
                unique[path] = lastmod
        entries = []
        for path, lastmod in unique.items():
            entries.append(f"<url><loc>{html_escape(site + path)}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>")
        self.send_xml('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(entries) + "</urlset>")

    def serve_public_seo_page(self, path):
        if path == "/updates": return self.send_html(seo_updates_page(path))
        records = seo_public_spots()
        if path == "/spots":
            return self.send_html(seo_listing_page(path, "전국 낚시 포인트 추천｜낚시터 지도·조과 – 물결포인트", "전국 바다·민물 낚시 포인트와 지역별 낚시터 정보를 확인하세요.", "전국 낚시 포인트 추천", records, "공개된 공식 낚시터와 사용자 공유 포인트를 유형과 지역별로 확인할 수 있습니다.", [("홈", "/"), ("낚시 포인트", path)]))
        if path in ("/spots/type/sea", "/spots/type/freshwater"):
            sea = path.endswith("sea")
            selected = [spot for spot in records if (spot.get("kind") == "sea") == sea]
            label = "바다낚시" if sea else "민물낚시"
            return self.send_html(seo_listing_page(path, f"{label} 포인트 추천｜지역별 낚시터 – 물결포인트", f"전국 {label} 포인트와 낚시터 정보를 확인하세요.", f"전국 {label} 포인트", selected, f"공개된 {label} 포인트 {len(selected)}곳을 확인할 수 있습니다.", [("홈", "/"), ("낚시 포인트", "/spots"), (f"{label} 포인트", path)]))
        if path.startswith("/spots/"):
            slug = path.rsplit("/", 1)[-1]
            if slug not in SEO_REGIONS: return self.send_seo_not_found()
            label, matchers = SEO_REGIONS[slug]
            selected = [spot for spot in records if any(matcher in f"{spot.get('title') or ''} {spot.get('address') or ''} {spot.get('description') or ''}" for matcher in matchers)]
            return self.send_html(seo_listing_page(path, f"{label} 낚시 포인트 추천｜낚시터 지도·조과 – 물결포인트", f"{label} 낚시 포인트와 공개 낚시터 정보를 확인하세요.", f"{label} 낚시 포인트 추천", selected, f"{label} 지역으로 분류된 공개 낚시터와 낚시 포인트 목록입니다.", [("홈", "/"), ("낚시 포인트", "/spots"), (f"{label} 낚시 포인트", path)]))
        if path.startswith("/spot/"):
            token = path.rsplit("-", 1)[-1]
            spot = next((item for item in records if seo_short_id(item.get("id")) == token), None)
            if not spot: return self.send_seo_not_found()
            return self.send_html(seo_detail_page(path, spot))
        if path == "/posts": return self.send_html(seo_catch_page(self.seo_posts(), records))
        if path == "/guides": return self.send_html(seo_guides_page(path))
        if path == "/guides/safe-fishing-basics": return self.send_html(seo_guide_article(path))
        return self.send_seo_not_found()

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        # 공개 첫 화면과 검색 엔진용 공개 페이지는 서버에서 완성된 HTML을 반환한다.
        # 인증·운영 API는 아래 기존 경로에서만 처리한다.
        if path in ("/", "/index.html"):
            return self.serve_main_page()
        if path == "/robots.txt":
            site = public_site_url()
            robots = f"User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\nDisallow: /login\nDisallow: /signup\nDisallow: /account/\nDisallow: /settings/\n\nSitemap: {site}/sitemap.xml\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            return self.wfile.write(robots.encode("utf-8"))
        if path == "/sitemap.xml":
            return self.serve_sitemap()
        # 예전 조과 URL을 새 게시글 목록으로 통합해 중복 색인을 막는다.
        if path == "/catch-reports":
            self.send_response(301)
            self.send_header("Location", "/posts")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        if path in ("/admin", "/admin/", "/admin.html"):
            if not self.require_admin(): return
            return self.send_html((ROOT / "admin.html").read_text(encoding="utf-8"), robots="noindex,nofollow,noarchive")
        if path in ("/spots", "/spots/type/sea", "/spots/type/freshwater", "/posts", "/guides", "/guides/safe-fishing-basics", "/updates") or path.startswith(("/spots/", "/spot/")):
            return self.serve_public_seo_page(path)
        # 배포 상태 확인용 경량 엔드포인트. 인증·외부 API·DB 조회를 하지 않는다.
        if self.path == "/api/health":
            return self.send_json({"ok": True})
        if self.path.startswith("/api/auth/me"):
            user = self.session_user()
            return self.send_json({"user": user})
        if self.path.startswith("/api/map-config"):
            return self.send_json({"ncpKeyId": os.environ.get("NAVER_MAPS_NCP_KEY_ID", "")})
        if self.path.startswith("/api/geocode"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("query", [""])[0].strip()
            if len(query) < 2: return self.send_json({"error": "두 글자 이상 입력해 주세요."}, 400)
            items, error = geocode(query)
            return self.send_json({"items": items} if items else {"items": [], "error": error or "검색 결과가 없습니다."}, 200 if items else 422)
        if self.path.startswith("/api/reverse-geocode"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                lat, lng = float(query.get("lat", [""])[0]), float(query.get("lng", [""])[0])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180): raise ValueError
            except (TypeError, ValueError):
                return self.send_json({"error": "올바른 좌표가 아닙니다."}, 400)
            address, error = reverse_geocode(lat, lng)
            return self.send_json({"address": address} if address else {"error": error or "주소를 찾지 못했습니다."}, 200 if address else 422)
        if self.path.startswith("/api/spots"):
            try: return self.send_json({"updatedAt": None, "items": supabase_request("official_spots?select=*&order=title.asc") or []})
            except RuntimeError: return self.send_json(read_json("official_spots.json", {"updatedAt": None, "items": []}))
        if self.path.startswith("/api/protected-areas"):
            try:
                rows = supabase_request("protected_areas?select=*&order=name.asc") or []
                return self.send_json({"type": "FeatureCollection", "updatedAt": None, "features": [{"type":"Feature", "properties":{"name":row["name"], "code":row.get("code", "")}, "geometry":row["geometry"]} for row in rows]})
            except RuntimeError: return self.send_json(read_json("protected_areas.geojson", {"type": "FeatureCollection", "updatedAt": None, "features": []}))
        if self.path.startswith("/api/sync"):
            if not self.require_admin(): return
            result = sync_all()
            return self.send_json(result, 200 if not result["errors"] else 502)
        if self.path.startswith("/api/admin/overview"):
            if not self.require_admin(): return
            try:
                reports = supabase_request("reports?select=*&order=created_at.desc&limit=100") or []
                inquiries = supabase_request("inquiries?select=*&order=created_at.desc&limit=100") or []
                spots = supabase_request("community_spots?select=*&order=created_at.desc&limit=50") or []
                posts = supabase_request("posts?select=id,is_hidden&order=created_at.desc&limit=200") or []
                audits = supabase_request("admin_audit_logs?select=*&order=created_at.desc&limit=50") or []
                member_count = confirmed_member_count()
                attach_member_labels(reports, inquiries, spots)
                post_visibility = {str(post["id"]): bool(post.get("is_hidden")) for post in posts}
                spot_visibility = {str(spot["id"]): bool(spot.get("is_hidden")) for spot in spots}
                for report in reports:
                    target_id = str(report.get("target_id") or "")
                    report["target_hidden"] = post_visibility.get(target_id, False) if report.get("kind") == "post" else spot_visibility.get(target_id, False)
                return self.send_json({"reports": reports, "inquiries": inquiries, "spots": spots, "audits": audits, "memberCount": member_count})
            except RuntimeError as error:
                return self.send_json({"error": str(error)}, 503)
        if self.path.startswith("/api/community-spots"):
            try:
                spots = supabase_request("community_spots?is_hidden=is.false&select=*&order=created_at.desc") or []
                attach_member_labels(spots)
                for spot in spots:
                    # 지도에는 공유자의 닉네임만 보이고, 이메일·내부 user_id는 응답에 포함하지 않는다.
                    spot["owner_name"] = spot.pop("reporter_name", "낚시꾼")
                    spot["owner_id"] = spot.pop("user_id", None)
                    spot["owner_role"] = spot.pop("reporter_role", "user")
                    spot.pop("reporter_key", None)
                return self.send_json({"items": spots})
            except RuntimeError as error:
                return self.send_json({"items": [], "error": str(error)}, 503)
        if self.path.startswith("/api/spot-recommendations"):
            try:
                rows = supabase_request("spot_recommendations?select=spot_id,user_id") or []
                counts = {}
                for row in rows:
                    spot_id = str(row.get("spot_id") or "")
                    counts[spot_id] = counts.get(spot_id, 0) + 1
                viewer = self.session_user()
                recommended = [str(row.get("spot_id")) for row in rows if viewer and str(row.get("user_id")) == viewer["id"]]
                return self.send_json({"counts": counts, "recommendedSpotIds": recommended})
            except RuntimeError as error:
                return self.send_json({"counts": {}, "recommendedSpotIds": [], "error": str(error)}, 503)
        if self.path.startswith("/api/angler-profile"):
            user_id = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [""])[0]
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id):
                return self.send_json({"error": "올바른 회원 정보가 아닙니다."}, 400)
            try:
                profiles = supabase_request(f"profiles?select=display_name&id=eq.{urllib.parse.quote(user_id, safe='')}") or []
                posts = supabase_request(f"posts?author_id=eq.{urllib.parse.quote(user_id, safe='')}&is_hidden=is.false&select=id,spot_id,author,species,length,created_at&order=length.desc.nullslast,created_at.desc&limit=100") or []
                hidden_spots = supabase_request("community_spots?is_hidden=is.true&select=id") or []
                hidden_ids = {str(item.get("id")) for item in hidden_spots}
                catches = []
                for post in posts:
                    try:
                        is_catch = float(post.get("length") or 0) > 0
                    except (TypeError, ValueError):
                        is_catch = False
                    if is_catch and str(post.get("spot_id")) not in hidden_ids:
                        catches.append(post)
                display_name = (profiles[0].get("display_name") if profiles else "") or (catches[0].get("author") if catches else "낚시꾼")
                return self.send_json({"id": user_id, "displayName": display_name, "totalCatches": len(catches), "topCatches": catches[:5]})
            except RuntimeError as error:
                return self.send_json({"error": str(error)}, 503)
        if path == "/api/species-rankings":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            species_id = query.get("speciesId", [""])[0]
            try:
                if species_id: species_id = str(uuid.UUID(species_id))
            except ValueError:
                return self.send_json({"error": "올바른 어종을 선택해 주세요."}, 400)
            viewer = self.session_user()
            try:
                result = supabase_request("rpc/get_species_rankings", "POST", {
                    "p_species_id": species_id or None,
                    "p_viewer_id": viewer["id"] if viewer else None,
                })
                result["viewerId"] = viewer["id"] if viewer else None
                return self.send_json(result)
            except RuntimeError:
                return self.send_json({"error": "어종별 랭킹을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."}, 503)
        if self.path.startswith("/api/community"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            spot_id = query.get("spotId", [""])[0]
            viewer = self.session_user()
            try:
                posts = supabase_request(f"posts?spot_id=eq.{urllib.parse.quote(spot_id, safe='')}&is_hidden=is.false&select=*&order=length.desc.nullslast,created_at.desc") if spot_id else supabase_request("posts?is_hidden=is.false&select=*&order=length.desc.nullslast,created_at.desc&limit=100")
                # 사용자 포인트 자체가 숨김 처리된 경우에도, 그 포인트에 연결된 조과는
                # 전체 랭킹·최근 조과에서 함께 제외한다.
                if not spot_id and posts:
                    hidden_spot_rows = supabase_request("community_spots?is_hidden=is.true&select=id") or []
                    hidden_spot_ids = {str(row.get("id")) for row in hidden_spot_rows}
                    posts = [post for post in posts if str(post.get("spot_id")) not in hidden_spot_ids]
                post_ids = [str(post.get("id")) for post in (posts or []) if post.get("id")]
                recommendations = []
                recommendations_available = True
                if post_ids:
                    encoded_ids = ",".join(urllib.parse.quote(post_id, safe="") for post_id in post_ids)
                    try:
                        recommendations = supabase_request(f"post_recommendations?post_id=in.({encoded_ids})&select=post_id,user_id") or []
                    except RuntimeError:
                        # 추천 테이블이 아직 배포되지 않았거나 일시적으로 조회되지 않아도
                        # 핵심 게시글·조과 목록은 정상적으로 보여준다.
                        recommendations_available = False
                counts = {str(post.get("id")): int(post.get("likes") or 0) for post in (posts or []) if post.get("id")}
                recommended_post_ids = []
                viewer_id = str(viewer.get("id") or "") if viewer else ""
                for recommendation in recommendations:
                    post_id = str(recommendation.get("post_id") or "")
                    if not post_id:
                        continue
                    counts[post_id] = counts.get(post_id, 0) + 1
                    if viewer_id and str(recommendation.get("user_id") or "") == viewer_id:
                        recommended_post_ids.append(post_id)
                return self.send_json({"posts": posts or [], "viewerId": viewer.get("id") if viewer else None, "recommendationCounts": counts, "recommendedPostIds": recommended_post_ids, "postRecommendationsAvailable": recommendations_available})
            except RuntimeError as error:
                return self.send_json({"posts": [], "error": str(error)}, 503)
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/cron-sync":
            # GitHub Actions가 예약 시간에만 호출하는 별도 동기화 진입점이다.
            # 사용자 세션 대신 두 환경에만 저장된 긴 임의 토큰을 비교한다.
            expected_token = os.environ.get("CRON_SYNC_TOKEN", "")
            provided_token = self.headers.get("X-Cron-Sync-Token", "")
            if len(expected_token) < 32 or not hmac.compare_digest(provided_token, expected_token):
                return self.send_json({"error": "예약 동기화 권한이 없습니다."}, 403)
            result = sync_all()
            return self.send_json(result, 200 if not result["errors"] else 502)
        if self.path.startswith("/api/auth/"):
            action = self.path.rsplit("/", 1)[-1]
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if action == "start-signup-verification":
                    email = str(payload.get("email", "")).strip()
                    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
                        return self.send_json({"error": "인증할 이메일을 올바르게 입력해 주세요."}, 400)
                    # 이메일을 확인하기 전에는 사용자가 지정한 비밀번호를 저장하지 않는다.
                    temporary_password = f"V!{uuid.uuid4().hex}{uuid.uuid4().hex}"
                    result = supabase_auth("signup", "POST", {"email": email, "password": temporary_password, "data": {"display_name": "인증 대기"}})
                    # 로컬 개발용으로 Supabase의 Confirm email을 끈 경우에는 즉시 인증된 세션이 반환된다.
                    access_token = (result or {}).get("access_token", "")
                    if access_token:
                        return self.send_json({"emailVerified": True, "email": ((result or {}).get("user") or {}).get("email", email)}, 200, {"Set-Cookie": f"wave_signup_verification={access_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=900"})
                    return self.send_json({"requiresEmailConfirmation": True, "email": ((result or {}).get("user") or {}).get("email", email)}, 202)
                if action == "signin":
                    result = supabase_auth("token?grant_type=password", "POST", {"email": str(payload.get("email", "")).strip(), "password": str(payload.get("password", ""))})
                    return self.set_session(result)
                if action == "resend-confirmation":
                    email = str(payload.get("email", "")).strip()
                    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
                        return self.send_json({"error": "인증 메일을 받을 이메일을 입력해 주세요."}, 400)
                    supabase_auth("resend", "POST", {"type": "signup", "email": email})
                    return self.send_json({"ok": True})
                if action == "request-password-reset":
                    email = str(payload.get("email", "")).strip().lower()
                    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
                        return self.send_json({"error": "이메일을 올바르게 입력해 주세요."}, 400)
                    allowed, retry_after = consume_rate_limit(f"password-reset:{email}", 5, 3600)
                    if not allowed:
                        return self.send_json({"error": f"재설정 메일 요청이 너무 많습니다. 약 {retry_after}초 후 다시 시도해 주세요."}, 429, {"Retry-After": str(retry_after)})
                    # 계정 존재 여부를 알려주지 않아 이메일 주소 추측을 막는다.
                    supabase_auth("recover", "POST", {"email": email, "redirect_to": self.password_reset_redirect_url()})
                    return self.send_json({"ok": True})
                if action == "reset-password":
                    access_token = str(payload.get("accessToken", "")).strip()
                    new_password = str(payload.get("newPassword", ""))
                    if not access_token:
                        return self.send_json({"error": "비밀번호 재설정 링크가 없거나 만료되었습니다. 다시 요청해 주세요."}, 401)
                    if len(new_password) < 8:
                        return self.send_json({"error": "새 비밀번호는 8자 이상 입력해 주세요."}, 400)
                    # recovery 토큰을 서버에서 검증한 뒤에만 새 비밀번호를 반영한다.
                    supabase_auth("user", "PUT", {"password": new_password}, access_token=access_token)
                    return self.send_json({"ok": True})
                if action == "verify-signup-email":
                    email, token = str(payload.get("email", "")).strip(), str(payload.get("token", "")).strip()
                    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email) or not token:
                        return self.send_json({"error": "이메일과 인증번호를 입력해 주세요."}, 400)
                    # 회원가입 확인 메일에서 발급된 OTP는 Supabase의 signup 유형으로 검증한다.
                    result = supabase_auth("verify", "POST", {"email": email, "token": token, "type": "signup"})
                    access_token = (result or {}).get("access_token", "")
                    if not access_token: return self.send_json({"error": "이메일 인증을 확인하지 못했습니다."}, 400)
                    return self.send_json({"emailVerified": True, "email": email}, 200, {"Set-Cookie": f"wave_signup_verification={access_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=900"})
                if action == "complete-signup":
                    nickname, password = str(payload.get("nickname", "")).strip()[:24], str(payload.get("password", ""))
                    token = self.pending_signup_token()
                    if not token: return self.send_json({"error": "이메일 인증부터 진행해 주세요."}, 401)
                    if not nickname or len(password) < 8:
                        return self.send_json({"error": "닉네임과 8자 이상 비밀번호를 입력해 주세요."}, 400)
                    user = supabase_auth("user", "PUT", {"password": password, "data": {"display_name": nickname}}, access_token=token)
                    supabase_request(f"profiles?id=eq.{urllib.parse.quote(str(user.get('id', '')), safe='')}", "PATCH", {"display_name": nickname})
                    return self.send_json({"ok": True}, 200, {"Set-Cookie": "wave_signup_verification=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
                if action == "signout":
                    self.clear_session_cookies()
                    return self.send_json({"ok": True})
                if action == "verify-account-password":
                    user = self.require_user()
                    if not user: return
                    password = str(payload.get("password", ""))
                    if not password: return self.send_json({"error": "현재 비밀번호를 입력해 주세요."}, 400)
                    result = supabase_auth("token?grant_type=password", "POST", {"email": user["email"], "password": password})
                    verified_user = (result or {}).get("user") or {}
                    if str(verified_user.get("id") or "") != user["id"]:
                        return self.send_json({"error": "본인 인증을 확인하지 못했습니다."}, 403)
                    self.queue_session_cookies(result)
                    self.queue_cookie("wave_account_verification", result.get("access_token", ""), 300)
                    return self.send_json({"ok": True, "expiresIn": 300})
                if action == "update-account":
                    user = self.require_user()
                    if not user: return
                    verification_token = self.account_verification_token()
                    if not verification_token: return self.send_json({"error": "변경 전 비밀번호로 본인 인증을 해주세요."}, 401)
                    verified_user = supabase_auth("user", access_token=verification_token)
                    if str(verified_user.get("id") or "") != user["id"]:
                        return self.send_json({"error": "본인 인증이 만료되었습니다. 다시 인증해 주세요."}, 401)
                    nickname = str(payload.get("nickname", "")).strip()[:24]
                    new_password = str(payload.get("newPassword", ""))
                    if not nickname and not new_password:
                        return self.send_json({"error": "변경할 닉네임 또는 비밀번호를 입력해 주세요."}, 400)
                    if nickname and len(nickname) < 2:
                        return self.send_json({"error": "닉네임은 2자 이상 입력해 주세요."}, 400)
                    if new_password and len(new_password) < 8:
                        return self.send_json({"error": "새 비밀번호는 8자 이상 입력해 주세요."}, 400)
                    update = {}
                    if nickname: update["data"] = {"display_name": nickname}
                    if new_password: update["password"] = new_password
                    supabase_auth("user", "PUT", update, access_token=verification_token)
                    if nickname:
                        supabase_request(f"profiles?id=eq.{urllib.parse.quote(user['id'], safe='')}", "PATCH", {"display_name": nickname})
                        # 게시글은 작성 당시의 닉네임도 함께 저장하므로, 프로필 변경 시
                        # author_id가 같은 과거 게시글·조과의 표시 이름을 모두 갱신한다.
                        supabase_request(f"posts?author_id=eq.{urllib.parse.quote(user['id'], safe='')}", "PATCH", {"author": nickname}, "return=minimal")
                    self.queue_cookie("wave_account_verification", "", 0)
                    refreshed = user_profile(supabase_auth("user", access_token=verification_token))
                    return self.send_json({"ok": True, "user": refreshed})
                if action == "delete-account":
                    user = self.require_user()
                    if not user: return
                    password = str(payload.get("password", ""))
                    if not password:
                        return self.send_json({"error": "현재 비밀번호를 입력해 주세요."}, 400)
                    # 탈퇴 요청마다 현재 비밀번호를 재확인한다. 회원정보 수정용 인증 쿠키만으로는 탈퇴할 수 없다.
                    verified = supabase_auth("token?grant_type=password", "POST", {"email": user["email"], "password": password})
                    verified_user = (verified or {}).get("user") or {}
                    if str(verified_user.get("id") or "") != user["id"]:
                        return self.send_json({"error": "본인 인증을 확인하지 못했습니다."}, 403)
                    deleted = delete_account_content(user)
                    self.clear_session_cookies()
                    self.queue_cookie("wave_account_verification", "", 0)
                    self.queue_cookie("wave_signup_verification", "", 0)
                    return self.send_json({"ok": True, "deleted": deleted})
            except (ValueError, json.JSONDecodeError, RuntimeError) as error:
                message = str(error)
                if "email rate limit exceeded" in message.lower():
                    message = "인증 메일 발송 한도를 초과했습니다. 잠시 후 다시 시도해 주세요."
                elif "email address not authorized" in message.lower():
                    message = "Supabase 기본 메일 서비스는 프로젝트 팀 이메일에만 발송합니다. 실제 사용자에게 보내려면 Custom SMTP를 설정해 주세요."
                elif "email not confirmed" in message.lower():
                    message = "이 이메일에는 이전 미인증 가입 기록이 있습니다. Supabase Authentication > Users에서 해당 사용자를 삭제한 뒤 다시 가입해 주세요."
                return self.send_json({"error": message}, 400)
            return self.send_json({"error": "지원하지 않는 인증 요청입니다."}, 404)
        if self.path.startswith("/api/migrate-to-supabase"):
            if not self.require_admin(): return
            try:
                return self.send_json(migrate_cached_official_data())
            except RuntimeError as error:
                return self.send_json({"error": str(error)}, 503)
        if self.path.startswith("/api/admin/"):
            user = self.require_admin()
            if not user: return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                action, target_id = self.path.rsplit("/", 1)[-1], str(payload.get("id", ""))
                note = str(payload.get("note", "")).strip()[:1000] or None
                if not target_id: return self.send_json({"error": "처리할 대상이 없습니다."}, 400)
                if not self.require_rate_limit(user, "admin-action", 60, 3600): return
                now = datetime.now(timezone.utc).isoformat()
                deleted_posts = 0
                resolved_reports = 0
                if action == "resolve-report":
                    supabase_request(f"reports?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"status": "resolved", "reviewed_at": now, "reviewed_by": user["id"], "admin_note": note})
                    target_type = "report"
                elif action == "resolve-inquiry":
                    supabase_request(f"inquiries?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"status": "resolved", "reviewed_at": now, "reviewed_by": user["id"], "admin_note": note})
                    target_type = "inquiry"
                elif action == "hide-post":
                    supabase_request(f"posts?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"is_hidden": True, "hidden_at": now, "hidden_by": user["id"]})
                    target_type = "post"
                elif action == "restore-post":
                    supabase_request(f"posts?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"is_hidden": False, "hidden_at": None, "hidden_by": None})
                    target_type = "post"
                elif action == "delete-post":
                    post_rows = supabase_request(f"posts?id=eq.{urllib.parse.quote(target_id, safe='')}&select=id,is_hidden,image_url") or []
                    if not post_rows: return self.send_json({"error": "삭제할 게시글을 찾지 못했습니다."}, 404)
                    if not post_rows[0].get("is_hidden"):
                        return self.send_json({"error": "게시글을 먼저 숨김 처리한 뒤에만 삭제할 수 있습니다."}, 400)
                    delete_post_image(post_rows[0].get("image_url"))
                    supabase_request(f"posts?id=eq.{urllib.parse.quote(target_id, safe='')}", "DELETE", None, "return=minimal")
                    pending_reports = supabase_request(f"reports?target_id=eq.{urllib.parse.quote(target_id, safe='')}&kind=eq.post&status=eq.pending&select=id") or []
                    if pending_reports:
                        supabase_request(f"reports?target_id=eq.{urllib.parse.quote(target_id, safe='')}&kind=eq.post&status=eq.pending", "PATCH", {"status": "resolved", "reviewed_at": now, "reviewed_by": user["id"], "admin_note": "대상 게시글 영구 삭제로 자동 처리 완료"})
                    resolved_reports = len(pending_reports)
                    target_type = "post"
                elif action == "hide-spot":
                    supabase_request(f"community_spots?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"is_hidden": True, "hidden_at": now, "hidden_by": user["id"]})
                    target_type = "spot"
                elif action == "restore-spot":
                    supabase_request(f"community_spots?id=eq.{urllib.parse.quote(target_id, safe='')}", "PATCH", {"is_hidden": False, "hidden_at": None, "hidden_by": None})
                    target_type = "spot"
                elif action == "delete-spot":
                    spot_rows = supabase_request(f"community_spots?id=eq.{urllib.parse.quote(target_id, safe='')}&select=id,is_hidden") or []
                    if not spot_rows: return self.send_json({"error": "삭제할 사용자 포인트를 찾지 못했습니다."}, 404)
                    if not spot_rows[0].get("is_hidden"):
                        return self.send_json({"error": "포인트를 먼저 숨김 처리한 뒤에만 삭제할 수 있습니다."}, 400)
                    linked_posts = supabase_request(f"posts?spot_id=eq.{urllib.parse.quote(target_id, safe='')}&select=id,image_url") or []
                    for post in linked_posts:
                        delete_post_image(post.get("image_url"))
                        supabase_request(f"posts?id=eq.{urllib.parse.quote(str(post['id']), safe='')}", "DELETE", None, "return=minimal")
                    deleted_posts = len(linked_posts)
                    supabase_request(f"community_spots?id=eq.{urllib.parse.quote(target_id, safe='')}", "DELETE", None, "return=minimal")
                    pending_reports = supabase_request(f"reports?target_id=eq.{urllib.parse.quote(target_id, safe='')}&kind=eq.spot&status=eq.pending&select=id") or []
                    if pending_reports:
                        automatic_note = "대상 포인트 영구 삭제로 자동 처리 완료"
                        supabase_request(f"reports?target_id=eq.{urllib.parse.quote(target_id, safe='')}&kind=eq.spot&status=eq.pending", "PATCH", {"status": "resolved", "reviewed_at": now, "reviewed_by": user["id"], "admin_note": automatic_note})
                    resolved_reports = len(pending_reports)
                    target_type = "spot"
                else: return self.send_json({"error": "지원하지 않는 운영자 작업입니다."}, 404)
                supabase_request("admin_audit_logs", "POST", {"actor_id": user["id"], "action": action, "target_type": target_type, "target_id": target_id, "note": note}, "return=minimal")
                return self.send_json({"ok": True, "deletedPosts": deleted_posts, "resolvedReports": resolved_reports})
            except (ValueError, json.JSONDecodeError, RuntimeError) as error:
                return self.send_json({"error": str(error)}, 400)
        if self.path.startswith("/api/community/"):
            user = self.require_user()
            if not user: return
            # Base64 전송은 원본 파일보다 약 33% 커진다. 사진 업로드만 50MB 원본을 허용한다.
            max_request_size = 72 * 1024 * 1024 if self.path.endswith("/upload") else 7 * 1024 * 1024
            if int(self.headers.get("Content-Length", "0")) > max_request_size:
                return self.send_json({"error": "요청 데이터가 너무 큽니다."}, 413)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "잘못된 요청입니다."}, 400)
            action = self.path.rsplit("/", 1)[-1]
            if action in ("post", "update-post"):
                try:
                    payload.update(catch_fields(payload))
                except ValueError as error:
                    return self.send_json({"error": str(error)}, 400)
            try:
                if action == "upload":
                    if not self.require_rate_limit(user, "upload", 10, 3600): return
                    return self.send_json({"imageUrl": upload_post_image(payload.get("dataUrl", ""))}, 201)
                if action == "post":
                    if not self.require_rate_limit(user, "post", 6, 600): return
                    item = {"id": str(uuid.uuid4()), "spot_id": str(payload.get("spotId", "")), "author": user["displayName"], "author_id": user["id"], "content": str(payload.get("content", ""))[:800], "species": str(payload.get("species", ""))[:30], "length": payload.get("length"), "length_is_ai": bool(payload.get("lengthIsAi")), "image_url": str(payload.get("imageUrl", ""))[:1000] or None}
                    if not item["spot_id"] or not item["content"]: return self.send_json({"error": "낚시터와 글 내용은 필수입니다."}, 400)
                    return self.send_json({"post": supabase_request("posts", "POST", item)[0]}, 201)
                if action == "update-post":
                    if not self.require_rate_limit(user, "update-post", 20, 3600): return
                    post_id = str(payload.get("postId", ""))
                    post_rows = supabase_request(f"posts?id=eq.{urllib.parse.quote(post_id, safe='')}&select=id,author_id,image_url") or []
                    if not post_rows: return self.send_json({"error": "수정할 게시글을 찾지 못했습니다."}, 404)
                    previous = post_rows[0]
                    if str(previous.get("author_id") or "") != user["id"]:
                        return self.send_json({"error": "본인이 작성한 게시글만 수정할 수 있습니다."}, 403)
                    content = str(payload.get("content", ""))[:800]
                    if not content: return self.send_json({"error": "게시글 내용을 입력해 주세요."}, 400)
                    new_image = str(payload.get("imageUrl", ""))[:1000]
                    if new_image and not new_image.startswith(f"{supabase_base_url()}/storage/v1/object/public/post-images/"):
                        return self.send_json({"error": "올바른 게시글 사진이 아닙니다."}, 400)
                    remove_image = bool(payload.get("removeImage"))
                    image_url = new_image or (None if remove_image else previous.get("image_url"))
                    item = {"content": content, "species": str(payload.get("species", ""))[:30], "length": payload.get("length"), "length_is_ai": False, "image_url": image_url}
                    updated = supabase_request(f"posts?id=eq.{urllib.parse.quote(post_id, safe='')}", "PATCH", item)
                    if previous.get("image_url") and previous.get("image_url") != image_url:
                        delete_post_image(previous.get("image_url"))
                    return self.send_json({"post": updated[0] if updated else item})
                if action == "delete-post":
                    post_id = str(payload.get("postId", ""))
                    post_rows = supabase_request(f"posts?id=eq.{urllib.parse.quote(post_id, safe='')}&select=id,author_id,image_url") or []
                    if not post_rows: return self.send_json({"error": "삭제할 게시글을 찾지 못했습니다."}, 404)
                    if str(post_rows[0].get("author_id") or "") != user["id"]:
                        return self.send_json({"error": "본인이 작성한 게시글만 삭제할 수 있습니다."}, 403)
                    delete_post_image(post_rows[0].get("image_url"))
                    supabase_request(f"posts?id=eq.{urllib.parse.quote(post_id, safe='')}", "DELETE", None, "return=minimal")
                    pending_reports = supabase_request(f"reports?target_id=eq.{urllib.parse.quote(post_id, safe='')}&kind=eq.post&status=eq.pending&select=id") or []
                    if pending_reports:
                        supabase_request(f"reports?target_id=eq.{urllib.parse.quote(post_id, safe='')}&kind=eq.post&status=eq.pending", "PATCH", {"status": "resolved", "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviewed_by": user["id"], "admin_note": "작성자 삭제로 자동 처리 완료"})
                    return self.send_json({"ok": True}, 200)
                if action == "spot":
                    if not self.require_rate_limit(user, "spot", 5, 86400): return
                    item = {"id": str(uuid.uuid4()), "user_id": user["id"], "title": str(payload.get("title", ""))[:32], "kind": str(payload.get("kind", "river")), "description": str(payload.get("description", ""))[:160], "address": str(payload.get("address", ""))[:240] or None, "species": "새 포인트", "lat": float(payload.get("lat")), "lng": float(payload.get("lng"))}
                    if not item["title"] or not item["description"]: return self.send_json({"error": "포인트 이름과 소개를 입력해주세요."}, 400)
                    saved_spot = supabase_request("community_spots", "POST", item)[0]
                    saved_spot["owner_name"] = user["displayName"]
                    saved_spot["owner_id"] = user["id"]
                    saved_spot["owner_role"] = user["role"]
                    saved_spot.pop("user_id", None)
                    return self.send_json({"spot": saved_spot}, 201)
                if action in ("report", "inquiry"):
                    if not self.require_rate_limit(user, action, 8 if action == "report" else 5, 3600): return
                    table = "reports" if action == "report" else "inquiries"
                    item = {"id": str(uuid.uuid4()), "user_id": user["id"], "kind": str(payload.get("kind", ""))[:30], "target_id": str(payload.get("targetId", ""))[:120], "reason": str(payload.get("reason", ""))[:80], "message": str(payload.get("message", ""))[:1200], "contact": str(payload.get("contact", ""))[:120]}
                    if not item["message"]: return self.send_json({"error": "내용을 입력해주세요."}, 400)
                    supabase_request(table, "POST", item)
                    return self.send_json({"ok": True}, 201)
                if action == "recommend-post":
                    if not self.require_rate_limit(user, "recommend-post", 100, 3600): return
                    try:
                        post_id = str(uuid.UUID(str(payload.get("postId", ""))))
                    except (ValueError, AttributeError):
                        return self.send_json({"error": "추천할 게시글을 찾지 못했습니다."}, 400)
                    encoded_post_id = urllib.parse.quote(post_id, safe="")
                    post_rows = supabase_request(f"posts?id=eq.{encoded_post_id}&is_hidden=is.false&select=id") or []
                    if not post_rows:
                        return self.send_json({"error": "추천할 수 없는 게시글입니다."}, 404)
                    encoded_user_id = urllib.parse.quote(user["id"], safe="")
                    existing = supabase_request(f"post_recommendations?post_id=eq.{encoded_post_id}&user_id=eq.{encoded_user_id}&select=id") or []
                    if existing:
                        supabase_request(f"post_recommendations?id=eq.{urllib.parse.quote(str(existing[0]['id']), safe='')}", "DELETE", None, "return=minimal")
                        recommended = False
                    else:
                        supabase_request("post_recommendations", "POST", {"post_id": post_id, "user_id": user["id"]})
                        recommended = True
                    count = len(supabase_request(f"post_recommendations?post_id=eq.{encoded_post_id}&select=id") or [])
                    return self.send_json({"recommended": recommended, "count": count})
                if action == "recommend-spot":
                    if not self.require_rate_limit(user, "recommend-spot", 100, 3600): return
                    spot_id = str(payload.get("spotId", ""))[:120]
                    if not spot_id: return self.send_json({"error": "추천할 포인트를 찾지 못했습니다."}, 400)
                    encoded_id = urllib.parse.quote(spot_id, safe='')
                    official = supabase_request(f"official_spots?id=eq.{encoded_id}&select=id") or []
                    community = supabase_request(f"community_spots?id=eq.{encoded_id}&select=id,is_hidden") or []
                    if not official and (not community or community[0].get("is_hidden")):
                        return self.send_json({"error": "추천할 수 없는 포인트입니다."}, 404)
                    existing = supabase_request(f"spot_recommendations?spot_id=eq.{encoded_id}&user_id=eq.{urllib.parse.quote(user['id'], safe='')}&select=id") or []
                    if existing:
                        supabase_request(f"spot_recommendations?id=eq.{urllib.parse.quote(str(existing[0]['id']), safe='')}", "DELETE", None, "return=minimal")
                        recommended = False
                    else:
                        supabase_request("spot_recommendations", "POST", {"spot_id": spot_id, "user_id": user["id"]})
                        recommended = True
                    count = len(supabase_request(f"spot_recommendations?spot_id=eq.{encoded_id}&select=id") or [])
                    return self.send_json({"recommended": recommended, "count": count})
            except RuntimeError as error:
                return self.send_json({"error": str(error)}, 503)
            return self.send_json({"error": "지원하지 않는 요청입니다."}, 404)
        if not self.path.startswith("/api/address-enrichment"):
            self.send_error(404)
            return
        # 본문 없이 호출하면 서버가 비밀키로 직접 좌표→주소 보완을 수행한다.
        # 클라이언트에는 Naver Maps 비밀키가 전달되지 않는다.
        if int(self.headers.get("Content-Length", "0")) == 0:
            data = read_json("official_spots.json", {"items": []})
            items = data.get("items", [])
            pending = sum(point.get("address") == "공식 API 등록 포인트" for point in items)
            errors = enrich_missing_addresses(items)
            write_json("official_spots.json", data)
            write_address_enrichment_memo(items)
            remaining = sum(point.get("address") == "공식 API 등록 포인트" for point in items)
            return self.send_json({"updated": pending - remaining, "remaining": remaining, "errors": errors[:3]})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            updates = {str(item["id"]): str(item["address"]) for item in payload.get("items", []) if item.get("id") and item.get("address")}
        except (ValueError, json.JSONDecodeError, AttributeError, KeyError):
            return self.send_json({"error": "잘못된 주소 보완 데이터입니다."}, 400)
        data = read_json("official_spots.json", {"items": []})
        changed = 0
        for point in data.get("items", []):
            address = updates.get(str(point.get("id")))
            if not address: continue
            point["address"] = address
            point["description"] = address
            point["addressSource"] = "naver-js-reverse-geocoding"
            changed += 1
        write_json("official_spots.json", data)
        write_address_enrichment_memo(data.get("items", []))
        return self.send_json({"updated": changed})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4173"))
    # 개발 기본값은 로컬 전용이다. 외부 공개 배포는 플랫폼 환경 변수로 HOST를 명시한다.
    host = os.environ.get("HOST", "127.0.0.1")
    sync_hour = int(os.environ.get("DAILY_SYNC_HOUR", "3"))
    sync_minute = int(os.environ.get("DAILY_SYNC_MINUTE", "30"))
    # Render에서는 Cron Job이 동기화를 담당한다. 로컬 개발에서는 기존 예약 실행을 유지한다.
    default_in_process_sync = "false" if os.environ.get("RENDER") else "true"
    run_in_process_sync = os.environ.get("RUN_IN_PROCESS_SYNC", default_in_process_sync).lower() in ("1", "true", "yes")
    print(f"http://{host}:{port} — 공식 데이터 동기화 시각은 매일 {sync_hour:02d}:{sync_minute:02d} KST입니다.")
    print("운영자는 긴급 갱신이 필요할 때만 /api/sync를 수동 실행할 수 있습니다.")
    if run_in_process_sync:
        threading.Thread(target=scheduled_sync, daemon=True).start()
    else:
        print("웹 서버의 예약 동기화는 비활성화됐습니다. Render Cron Job이 동기화를 실행합니다.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
