#!/usr/bin/env python3
"""물결포인트 로컬/API 서버 — 키는 .env에서만 읽습니다."""
from __future__ import annotations

import json
import base64
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

def upload_post_image(data_url):
    match = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", data_url or "")
    if not match: raise RuntimeError("JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.")
    binary = base64.b64decode(match.group(2), validate=True)
    if len(binary) > 5 * 1024 * 1024: raise RuntimeError("사진은 5MB 이하만 업로드할 수 있습니다.")
    url = supabase_base_url()
    key = supabase_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERV_ROLE_KEY")
    suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[match.group(1)]
    name = f"posts/{uuid.uuid4()}.{suffix}"
    request = urllib.request.Request(f"{url}/storage/v1/object/post-images/{name}", data=binary, method="POST", headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": match.group(1), "x-upsert": "false"})
    try:
        with urllib.request.urlopen(request, timeout=25): pass
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"사진 업로드 실패 (HTTP {error.code})") from error
    return f"{url}/storage/v1/object/public/post-images/{name}"

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

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # 정적 파일과 API 응답 모두에 적용되는 기본 브라우저 보안 정책이다.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
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

    def do_GET(self):
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
                attach_member_labels(reports, inquiries, spots)
                post_visibility = {str(post["id"]): bool(post.get("is_hidden")) for post in posts}
                spot_visibility = {str(spot["id"]): bool(spot.get("is_hidden")) for spot in spots}
                for report in reports:
                    target_id = str(report.get("target_id") or "")
                    report["target_hidden"] = post_visibility.get(target_id, False) if report.get("kind") == "post" else spot_visibility.get(target_id, False)
                return self.send_json({"reports": reports, "inquiries": inquiries, "spots": spots, "audits": audits})
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
                return self.send_json({"posts": posts or [], "viewerId": viewer.get("id") if viewer else None})
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
            except (ValueError, json.JSONDecodeError, RuntimeError) as error:
                message = str(error)
                if "email rate limit exceeded" in message.lower():
                    message = "인증 메일 발송 한도를 초과했습니다. 잠시 후 다시 시도해 주세요."
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
                    linked_posts = supabase_request(f"posts?spot_id=eq.{urllib.parse.quote(target_id, safe='')}&select=id") or []
                    for post in linked_posts:
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
            if int(self.headers.get("Content-Length", "0")) > 7 * 1024 * 1024:
                return self.send_json({"error": "요청 데이터가 너무 큽니다."}, 413)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "잘못된 요청입니다."}, 400)
            action = self.path.rsplit("/", 1)[-1]
            try:
                if action == "upload":
                    if not self.require_rate_limit(user, "upload", 10, 3600): return
                    return self.send_json({"imageUrl": upload_post_image(payload.get("dataUrl", ""))}, 201)
                if action == "post":
                    if not self.require_rate_limit(user, "post", 6, 600): return
                    item = {"id": str(uuid.uuid4()), "spot_id": str(payload.get("spotId", "")), "author": user["displayName"], "author_id": user["id"], "content": str(payload.get("content", ""))[:800], "species": str(payload.get("species", ""))[:30], "length": payload.get("length"), "length_is_ai": bool(payload.get("lengthIsAi")), "image_url": str(payload.get("imageUrl", ""))[:1000] or None}
                    if not item["spot_id"] or not item["content"]: return self.send_json({"error": "낚시터와 글 내용은 필수입니다."}, 400)
                    return self.send_json({"post": supabase_request("posts", "POST", item)[0]}, 201)
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
                if action == "like":
                    result = supabase_request("rpc/increment_post_like", "POST", {"post_id": str(payload.get("postId", ""))})
                    return self.send_json({"likes": result})
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
