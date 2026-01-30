# main.py (Render 경량화 버전: Playwright 제거 / requests 사용)
# ------------------------------------------------------------------------------------
# 변경사항(요청 반영)
# 1) 계약종료 라이더 제외: accountStatus.code 가 종료 계열이면 목록/조회/대시보드에서 제외
# 2) 로그인용 "가상 뒷4" 지원:
#    - 관리자가 대시보드에서 가상 뒷4 지정/변경 가능
#    - 개인 조회(/check)는 "이름 + 가상 뒷4"로만 로그인/조회
# 3) 개인정보 최소노출: 개인 결과 화면은 마스킹 전화번호 유지
# 4) Render 경량화: Playwright 제거(브라우저 설치 불필요), requests + BAEMIN_COOKIE 사용
# 5) 세션만료(쿠키 오류) 시 500 대신 안내 페이지로 처리
#
# 설치:
#   pip install -r requirements.txt
#
# 로컬 실행:
#   set BAEMIN_CENTER_ID=DP2510205467
#   set BAEMIN_COOKIE=... (쿠키 문자열)
#   uvicorn main:app --reload --host 0.0.0.0 --port 8000
# ------------------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware


# -----------------------------
# Config
# -----------------------------
BASE_API = "https://api-deliverycenter.baemin.com"

RIDERS_CACHE_TTL = 60
STATUS_CACHE_TTL = 30

RATE_WINDOW_SEC = 60
RATE_MAX_REQ = 30

# Render에서는 환경변수로 바꾸는 걸 권장
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "0315")
SESSION_SECRET = os.getenv("SESSION_SECRET", "rider-welfare-admin-secret")

OVERRIDE_FILE = "join_overrides.json"     # key: "normname|login4" -> "YYYY-MM-DD"
LOGIN4_FILE = "login4_overrides.json"     # key: "normname|real4"  -> "login4"  (가상뒷4)

_override_lock = threading.Lock()
_login4_lock = threading.Lock()

_rate_bucket: Dict[str, List[float]] = {}
_riders_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_status_cache: Dict[str, Any] = {}

app = FastAPI(title="라웰 등급 조회 (Requests)")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# -----------------------------
# Helpers
# -----------------------------
def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding:16px; background:#fafafa;">
  <div style="max-width:1200px; margin:0 auto;">
    {body}
  </div>
</body>
</html>"""


def norm_name(x: str) -> str:
    return re.sub(r"\s+", "", (x or "")).strip().lower()


def last4_from_phone(phone: str) -> str:
    m = re.search(r"(\d{4})\s*$", (phone or "").replace(" ", ""))
    return m.group(1) if m else ""


def mask_phone(phone: str) -> str:
    p = phone or ""
    m = re.search(r"(\d{2,3})-?(\d{3,4})-?(\d{4})", p)
    if not m:
        return p
    a, b, c = m.group(1), m.group(2), m.group(3)
    return f"{a}-****-{c}"


def rate_limit(ip: str) -> bool:
    now = time.time()
    arr = _rate_bucket.get(ip, [])
    arr = [t for t in arr if now - t <= RATE_WINDOW_SEC]
    if len(arr) >= RATE_MAX_REQ:
        _rate_bucket[ip] = arr
        return False
    arr.append(now)
    _rate_bucket[ip] = arr
    return True


def safe_date_parse(s: str) -> Optional[date]:
    s = (s or "").strip()
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def is_ended_contract(rider: Dict[str, Any]) -> bool:
    st = rider.get("accountStatus") or {}
    code = (st.get("code") or "").upper()
    desc = (st.get("desc") or "")
    if "END" in code or "TERMIN" in code or "EXPIRE" in code:
        return True
    if "계약" in desc and "종료" in desc:
        return True
    return False


# -----------------------------
# Grade rules
# -----------------------------
def grade_from_total(total: int) -> str:
    if total <= 479:
        return "무등급"
    if total <= 719:
        return "R5"
    if total <= 959:
        return "R4"
    if total <= 1199:
        return "R3"
    if total <= 1439:
        return "R2"
    return "R1"


def next_grade_target(total: int) -> Tuple[Optional[str], Optional[int]]:
    thresholds = [
        ("무등급", 0),
        ("R5", 480),
        ("R4", 720),
        ("R3", 960),
        ("R2", 1200),
        ("R1", 1440),
    ]
    cur = grade_from_total(total)
    idx = [g for g, _ in thresholds].index(cur)
    if cur == "R1":
        return None, None
    nxt_g, nxt_t = thresholds[idx + 1]
    return nxt_g, max(0, nxt_t - total)


# -----------------------------
# Period logic (join day based) - "마감일 포함"
# -----------------------------
def clamp_day(year: int, month: int, target_day: int) -> date:
    first = date(year, month, 1)
    last_day = (first + relativedelta(months=1) - timedelta(days=1)).day
    return date(year, month, min(target_day, last_day))


def current_period(join_date: date, today: date) -> Tuple[date, date]:
    join_day = join_date.day
    this_anchor = clamp_day(today.year, today.month, join_day)

    if today >= this_anchor:
        start_d = this_anchor
    else:
        prev = date(today.year, today.month, 1) + relativedelta(months=-1)
        start_d = clamp_day(prev.year, prev.month, join_day)

    next_m = date(start_d.year, start_d.month, 1) + relativedelta(months=1)
    end_inclusive = clamp_day(next_m.year, next_m.month, join_day)
    return start_d, end_inclusive


def period_to_from_to(start_d: date, end_inclusive: date) -> Tuple[date, date]:
    api_max = date.today() - timedelta(days=1)
    from_d = start_d
    to_d = min(end_inclusive, api_max)
    if from_d > to_d:
        from_d = to_d
    return from_d, to_d


# -----------------------------
# Join-date override (persistent) keyed by (name|login4)
# -----------------------------
def load_overrides() -> Dict[str, str]:
    with _override_lock:
        if not os.path.exists(OVERRIDE_FILE):
            return {}
        try:
            with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_overrides(data: Dict[str, str]) -> None:
    with _override_lock:
        with open(OVERRIDE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# -----------------------------
# Login4 override (persistent)
# -----------------------------
def load_login4_map() -> Dict[str, str]:
    with _login4_lock:
        if not os.path.exists(LOGIN4_FILE):
            return {}
        try:
            with open(LOGIN4_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {}
        except Exception:
            return {}


def save_login4_map(data: Dict[str, str]) -> None:
    with _login4_lock:
        with open(LOGIN4_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_login4_for_rider(rider: Dict[str, Any]) -> Tuple[str, str, str]:
    nm = rider.get("name") or ""
    ph = rider.get("phoneNumber") or ""
    real4 = last4_from_phone(ph)
    k_real = f"{norm_name(nm)}|{real4}"

    m = load_login4_map()
    if k_real in m and re.fullmatch(r"\d{4}", (m[k_real] or "").strip()):
        return m[k_real].strip(), real4, "override"
    return real4, real4, "real"


def set_login4_override(name_norm: str, real4: str, login4: str) -> None:
    m = load_login4_map()
    m[f"{name_norm}|{real4}"] = login4
    save_login4_map(m)


def clear_login4_override(name_norm: str, real4: str) -> None:
    m = load_login4_map()
    k = f"{name_norm}|{real4}"
    if k in m:
        del m[k]
    save_login4_map(m)


def get_effective_join_date_by_login_key(rider: Dict[str, Any], login4: str) -> Tuple[date, str]:
    nm = rider.get("name") or ""
    key = f"{norm_name(nm)}|{login4}"

    ov = load_overrides().get(key)
    if ov:
        d = safe_date_parse(ov)
        if d:
            return d, "override"

    created_raw = rider.get("createdDate")
    if isinstance(created_raw, str) and len(created_raw) >= 10:
        return date.fromisoformat(created_raw[:10]), "createdDate"

    return date.today(), "fallback"


# -----------------------------
# Render 환경변수
# -----------------------------
def require_center_id() -> str:
    center_id = (os.getenv("BAEMIN_CENTER_ID") or "").strip()
    if not center_id:
        raise RuntimeError("BAEMIN_CENTER_ID가 없습니다. Render Settings > Environment에 추가하세요.")
    return center_id


def require_cookie() -> str:
    cookie = (os.getenv("BAEMIN_COOKIE") or "").strip()
    if not cookie:
        raise RuntimeError("BAEMIN_COOKIE가 없습니다. Render Settings > Environment에 추가하세요.")
    return cookie


# -----------------------------
# HTTP (requests)
# -----------------------------
_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://deliverycenter.baemin.com",
    "Referer": "https://deliverycenter.baemin.com/",
})


import os
import requests

BASE_API = "https://api-deliverycenter.baemin.com"

def _clean_cookie(raw: str) -> str:
    # Render 환경변수 textarea에서 줄바꿈/공백이 섞이면 헤더가 깨질 수 있어서 정리
    if not raw:
        return ""
    return raw.replace("\r", " ").replace("\n", " ").strip()

def _build_headers() -> dict:
    center_id = (os.getenv("BAEMIN_CENTER_ID") or "").strip()
    cookie = _clean_cookie(os.getenv("BAEMIN_COOKIE") or "")

    if not center_id or not cookie:
        # 너가 만든 “세션 만료/설정 오류” 화면으로 보내는 트리거로 쓰면 됨
        raise PermissionError("SESSION_EXPIRED")

    # DevTools에서 확인한 필수급 헤더들
    return {
        "accept": "application/json, text/plain, */*",
        "origin": "https://deliverycenter.baemin.com",
        "referer": "https://deliverycenter.baemin.com/",
        "user-agent": os.getenv(
            "BAEMIN_UA",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        ),
        "center-id": center_id,
        "cookie": cookie,
    }

def api_get(url: str, params: dict | None = None):
    headers = _build_headers()

    # 세션 유지/재사용(가끔 클라우드플레어가 세션/쿠키에 민감)
    with requests.Session() as s:
        r = s.get(url, params=params, headers=headers, timeout=20)
        print("API_GET", url, "status=", r.status_code, "len=", len(r.text))
        print("API_BODY_HEAD", r.text[:200])


    # ✅ 여기! requests는 status가 아니라 status_code
    if r.status_code in (401, 403):
        raise PermissionError("SESSION_EXPIRED")

    if r.status_code >= 400:
        # 디버깅용: 앞부분만
        raise RuntimeError(f"API_HTTP_{r.status_code}: {r.text[:200]}")

    # JSON 파싱
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"API_NON_JSON: {r.text[:200]}")



# -----------------------------
# Baemin API wrappers (cached)
# -----------------------------
def fetch_riders_cached() -> List[Dict[str, Any]]:
    now = time.time()
    if _riders_cache["data"] is not None and now - _riders_cache["ts"] <= RIDERS_CACHE_TTL:
        return _riders_cache["data"]

    params = {
        "name": "",
        "userId": "",
        "phoneNumber": "",
        "accountStatus": "",
        "orderName": "",
        "orderBy": "",
    }

    j = api_get(f"{BASE_API}/rider", params=params)

    if isinstance(j, list):
        items = j
    elif isinstance(j, dict):
        items = j.get("items") or j.get("data") or []
    else:
        items = []

    items = [r for r in items if not is_ended_contract(r)]

    _riders_cache["ts"] = now
    _riders_cache["data"] = items
    return items


def fetch_status_complete_map_cached(from_d: date, to_d: date) -> Dict[str, int]:
    today = date.today()
    if to_d >= today:
        to_d = today - timedelta(days=1)
    if from_d > to_d:
        from_d = to_d

    key_cache = f"{from_d.isoformat()}_{to_d.isoformat()}"
    now = time.time()
    cached = _status_cache.get(key_cache)
    if cached and now - cached["ts"] <= STATUS_CACHE_TTL:
        return cached["data"]

    complete: Dict[str, int] = {}
    size = 100
    page = 0
    max_pages = 600

    while page < max_pages:
        params = {
            "page": page,
            "size": size,
            "fromDate": from_d.isoformat(),
            "toDate": to_d.isoformat(),
        }

        j = api_get(f"{BASE_API}/management/rider-delivery-status", params=params)

        rows = (j.get("data") or []) if isinstance(j, dict) else []
        if not rows:
            break

        for it in rows:
            nm = it.get("name") or ""
            ph = it.get("phoneNumber") or ""
            real4 = last4_from_phone(ph)
            k = f"{norm_name(nm)}|{real4}"
            cnt = (it.get("deliveryAcceptanceCount") or {}).get("complete") or 0
            if k and real4:
                complete[k] = int(cnt)

        page += 1

    _status_cache[key_cache] = {"ts": now, "data": complete}
    return complete


# -----------------------------
# Admin helpers
# -----------------------------
def require_admin(request: Request) -> Optional[RedirectResponse]:
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin-login", status_code=303)
    return None


def session_expired_page() -> HTMLResponse:
    body = """
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:640px; margin:0 auto;">
      <h3 style="margin-top:0;">세션(쿠키) 만료 또는 설정 오류</h3>
      <div style="color:#666; line-height:1.6;">
        현재 서버가 배민 API에 접근할 쿠키(BAEMIN_COOKIE)가 없거나 만료되었습니다.<br/>
        <b>Render → Settings → Environment</b>에서<br/>
        <b>BAEMIN_CENTER_ID</b>, <b>BAEMIN_COOKIE</b> 를 다시 설정 후 재배포하세요.
      </div>
      <div style="margin-top:12px;">
        <a href="/" style="text-decoration:none; color:#111;">← 홈</a>
        &nbsp;&nbsp;
        <a href="/health" style="text-decoration:none; color:#666;">헬스체크</a>
      </div>
    </div>
    """
    return HTMLResponse(html_page("세션 만료", body), status_code=200)


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    body = """
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
      <h2 style="margin:0 0 6px 0;">라웰 등급 조회</h2>
      <div style="color:#666; margin-bottom:14px;">이름 + <b>로그인용 뒷4자리</b>로 조회합니다. (관리자가 설정)</div>

      <form method="post" action="/check">
        <div style="margin-bottom:12px;">
          <label style="display:block; margin-bottom:6px;">이름</label>
          <input name="name" autocomplete="name"
                 style="font-size:18px; padding:12px; width:100%; box-sizing:border-box; border:1px solid #ddd; border-radius:12px;"
                 required />
        </div>

        <div style="margin-bottom:14px;">
          <label style="display:block; margin-bottom:6px;">로그인용 뒷 4자리</label>
          <input name="login4" inputmode="numeric" pattern="\\d{4}" maxlength="4"
                 style="font-size:18px; padding:12px; width:180px; border:1px solid #ddd; border-radius:12px;"
                 required />
        </div>

        <button type="submit"
                style="font-size:18px; padding:12px 16px; border:none; border-radius:12px; background:#111; color:#fff; width:100%;">
          조회
        </button>
      </form>

      <div style="display:flex; justify-content:space-between; margin-top:14px; font-size:14px;">
        <a href="/dashboard" style="text-decoration:none; color:#111;">관리자: 전체현황</a>
        <a href="/admin" style="text-decoration:none; color:#666;">관리자 도움말</a>
      </div>

      <div style="color:#888; margin-top:12px; font-size:13px;">
        * 완료건수는 ‘어제까지’ 반영됩니다.
      </div>
    </div>
    """
    return html_page("라웰 등급 조회", body)


@app.get("/check")
def check_get_redirect():
    return RedirectResponse(url="/", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
def admin_help():
    body = """
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:720px; margin:0 auto;">
      <h2 style="margin:0 0 6px 0;">관리자</h2>
      <div style="color:#666; margin-bottom:12px;">
        Render에서 <b>BAEMIN_COOKIE</b>가 만료되면 다시 갱신해서 환경변수에 넣어야 합니다.
      </div>

      <div style="background:#f7f7f7; border-radius:12px; padding:12px; font-size:14px; line-height:1.5;">
        <div><b>Render 설정</b></div>
        <div>- Settings → Environment → BAEMIN_CENTER_ID / BAEMIN_COOKIE 등록</div>
        <div>- Start Command: <span style="font-family: ui-monospace;">uvicorn main:app --host 0.0.0.0 --port $PORT</span></div>
      </div>

      <div style="margin-top:14px;">
        <a href="/" style="text-decoration:none; color:#111;">← 조회 화면으로</a>
      </div>
    </div>
    """
    return html_page("관리자", body)


@app.post("/check", response_class=HTMLResponse)
def check(request: Request, name: str = Form(...), login4: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if not rate_limit(ip):
        body = """
        <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
          <h3 style="margin-top:0;">요청이 너무 많습니다</h3>
          <div style="color:#666;">잠시 후 다시 시도해주세요.</div>
          <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 뒤로</a></div>
        </div>
        """
        return html_page("제한됨", body)

    name_in = norm_name(name)
    login4 = (login4 or "").strip()

    if not re.fullmatch(r"\d{4}", login4):
        body = """
        <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
          <h3 style="margin-top:0;">입력 오류</h3>
          <div style="color:#666;">뒷 4자리는 숫자 4자리로 입력해주세요.</div>
          <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 뒤로</a></div>
        </div>
        """
        return html_page("입력 오류", body)

    try:
        riders = fetch_riders_cached()
    except PermissionError:
        return session_expired_page()
    except RuntimeError:
        return session_expired_page()

    candidates = [r for r in riders if norm_name(r.get("name", "")) == name_in]
    matches: List[Dict[str, Any]] = []
    for r in candidates:
        l4, real4, src = get_login4_for_rider(r)
        if l4 == login4:
            matches.append(r)

    if not matches:
        body = f"""
        <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
          <h3 style="margin-top:0;">조회 결과 없음</h3>
          <div style="color:#666;">입력: <b>{name}</b> / <b>{login4}</b></div>
          <div style="color:#888; margin-top:8px; font-size:13px;">이름(띄어쓰기/철자) 또는 로그인용 뒷4를 확인해주세요.</div>
          <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 다시 조회</a></div>
        </div>
        """
        return html_page("조회 결과 없음", body)

    if len(matches) >= 2:
        body = """
        <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
          <h3 style="margin-top:0;">동일 정보 다수</h3>
          <div style="color:#666;">동일 이름/로그인용뒷4가 여러 명입니다. 관리자에게 문의해주세요.</div>
          <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 뒤로</a></div>
        </div>
        """
        return html_page("동일 정보 다수", body)

    rider = matches[0]
    rider_login4, rider_real4, login_src = get_login4_for_rider(rider)

    eff_join_date, join_src = get_effective_join_date_by_login_key(rider, rider_login4)

    today = date.today()

    cur_start, cur_end_incl = current_period(eff_join_date, today)
    cur_from, cur_to = period_to_from_to(cur_start, cur_end_incl)

    prev_end_incl = cur_start - timedelta(days=1)
    prev_start = cur_start - relativedelta(months=1)
    prev_from, prev_to = period_to_from_to(prev_start, prev_end_incl)

    try:
        cmap_cur = fetch_status_complete_map_cached(cur_from, cur_to)
        cmap_prev = fetch_status_complete_map_cached(prev_from, prev_to)
    except PermissionError:
        return session_expired_page()
    except RuntimeError:
        return session_expired_page()

    api_key = f"{name_in}|{rider_real4}"
    cur_completed = int(cmap_cur.get(api_key, 0))
    prev_completed = int(cmap_prev.get(api_key, 0))

    planned_grade = grade_from_total(cur_completed)
    current_grade = grade_from_total(prev_completed)
    nxt, remain = next_grade_target(cur_completed)

    join_note = "관리자 설정" if join_src == "override" else "배민 입사일"

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:780px; margin:0 auto;">
      <h2 style="margin:0 0 6px 0;">등급 조회 결과</h2>

      <div style="color:#888; font-size:13px; margin-top:6px;">
        기준일(입사일): <b>{eff_join_date}</b> ({join_note})
      </div>

      <div style="margin-top:12px; padding:12px; border:1px solid #eee; border-radius:14px; background:#fcfcfc;">
        <div style="font-size:18px;"><b>{rider.get('name','')}</b> 님</div>
        <div style="color:#777; margin-top:6px;">휴대폰: {mask_phone(rider.get('phoneNumber',''))}</div>
      </div>

      <div style="display:flex; gap:12px; margin-top:12px; flex-wrap:wrap;">
        <div style="flex:1; min-width:230px; padding:12px; border-radius:12px; border:1px solid #eee; background:#fff;">
          <div style="color:#777; font-size:13px;">현재등급(직전기간)</div>
          <div style="font-size:32px; font-weight:900; line-height:1.1;">{current_grade}</div>
          <div style="font-size:12px; color:#999; margin-top:6px;">
            정책기간: {prev_start} ~ {prev_end_incl}<br/>
            반영기간(API): {prev_from} ~ {prev_to} / 완료 {prev_completed}건
          </div>
        </div>

        <div style="flex:1; min-width:230px; padding:12px; border-radius:12px; border:1px solid #eee; background:#fff;">
          <div style="color:#777; font-size:13px;">예정등급(현재기간)</div>
          <div style="font-size:32px; font-weight:900; line-height:1.1;">{planned_grade}</div>
          <div style="font-size:12px; color:#999; margin-top:6px;">
            정책기간: {cur_start} ~ {cur_end_incl}<br/>
            반영기간(API): {cur_from} ~ {cur_to} / 완료 {cur_completed}건
          </div>
        </div>
      </div>

      <div style="margin-top:12px; padding:12px; border:1px solid #eee; border-radius:14px; background:#fff;">
        <div style="color:#666;">
          다음등급: <b>{(nxt or '-')}</b> / 남은건수: <b>{(remain if remain is not None else '-')}</b>
        </div>
        <div style="color:#999; font-size:12px; margin-top:6px;">* 다음등급/남은건수는 “예정등급(현재기간)” 기준입니다.</div>
      </div>

      <div style="margin-top:14px;">
        <a href="/" style="text-decoration:none; color:#111;">← 다시 조회</a>
      </div>
    </div>
    """
    return html_page("등급 조회 결과", body)


# -----------------------------
# Admin login
# -----------------------------
@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_page():
    body = """
    <div style="max-width:420px; margin:80px auto; background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:20px;">
      <h2 style="margin-top:0;">관리자 로그인</h2>
      <form method="post" action="/admin-login">
        <input type="password" name="password" placeholder="비밀번호"
               style="width:100%; font-size:18px; padding:12px; border:1px solid #ddd; border-radius:12px;" required />
        <button type="submit"
                style="width:100%; margin-top:12px; font-size:18px; padding:12px;
                       border:none; border-radius:12px; background:#111; color:#fff;">
          로그인
        </button>
      </form>
      <div style="margin-top:12px;">
        <a href="/" style="color:#666; text-decoration:none;">← 메인으로</a>
      </div>
    </div>
    """
    return html_page("관리자 로그인", body)


@app.post("/admin-login")
def admin_login_action(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        request.session["is_admin"] = True
        return RedirectResponse("/dashboard", status_code=303)

    body = """
    <div style="max-width:420px; margin:80px auto; background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:20px;">
      <h3 style="margin-top:0;">비밀번호가 틀렸습니다</h3>
      <div style="color:#666;">다시 시도해주세요.</div>
      <div style="margin-top:12px;"><a href="/admin-login" style="text-decoration:none; color:#111;">다시 로그인</a></div>
      <div style="margin-top:10px;"><a href="/" style="text-decoration:none; color:#666;">← 메인으로</a></div>
    </div>
    """
    return HTMLResponse(html_page("로그인 실패", body))


@app.get("/admin-logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# -----------------------------
# Admin: join/login4 set/clear
# -----------------------------
@app.post("/admin/set-join")
def admin_set_join(request: Request, key: str = Form(...), join_date: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    jd = safe_date_parse(join_date)
    if not key or jd is None:
        return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)

    data = load_overrides()
    data[key] = jd.isoformat()
    save_overrides(data)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/clear-join")
def admin_clear_join(request: Request, key: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    data = load_overrides()
    if key in data:
        del data[key]
        save_overrides(data)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/set-login4")
def admin_set_login4(request: Request, name_norm: str = Form(...), real4: str = Form(...), login4: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    name_norm = (name_norm or "").strip()
    real4 = (real4 or "").strip()
    login4 = (login4 or "").strip()

    if not name_norm or not re.fullmatch(r"\d{4}", real4) or not re.fullmatch(r"\d{4}", login4):
        return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)

    set_login4_override(name_norm, real4, login4)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/clear-login4")
def admin_clear_login4(request: Request, name_norm: str = Form(...), real4: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    name_norm = (name_norm or "").strip()
    real4 = (real4 or "").strip()
    if not name_norm or not re.fullmatch(r"\d{4}", real4):
        return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)

    clear_login4_override(name_norm, real4)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


# -----------------------------
# Dashboard (admin only)
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, q: str = ""):
    r = require_admin(request)
    if r:
        return r

    try:
        riders = fetch_riders_cached()
    except PermissionError:
        return session_expired_page()
    except RuntimeError:
        return session_expired_page()

    join_overrides = load_overrides()

    qn = norm_name(q)

    rider_rows = []
    for rr in riders:
        nm = rr.get("name") or ""
        ph = rr.get("phoneNumber") or ""
        real4 = last4_from_phone(ph)
        if not real4:
            continue
        if qn and (qn not in (norm_name(nm) + real4)):
            continue
        rider_rows.append(rr)

    today = date.today()

    cur_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}
    prev_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}

    computed_rows: List[Dict[str, Any]] = []

    for rr in rider_rows:
        nm = rr.get("name") or ""
        ph = rr.get("phoneNumber") or ""
        real4 = last4_from_phone(ph)
        nn = norm_name(nm)

        login4, _, login_src = get_login4_for_rider(rr)
        real_key = f"{nn}|{real4}"
        login_key = f"{nn}|{login4}"

        eff_join, join_src = get_effective_join_date_by_login_key(rr, login4)

        cur_start, cur_end_incl = current_period(eff_join, today)
        cur_from, cur_to = period_to_from_to(cur_start, cur_end_incl)

        prev_end_incl = cur_start - timedelta(days=1)
        prev_start = cur_start - relativedelta(months=1)
        prev_from, prev_to = period_to_from_to(prev_start, prev_end_incl)

        item = {
            "rider": rr,
            "nn": nn,
            "real4": real4,
            "login4": login4,
            "login_src": login_src,
            "real_key": real_key,
            "login_key": login_key,
            "eff_join": eff_join,
            "join_src": join_src,
            "cur_start": cur_start,
            "cur_end_incl": cur_end_incl,
            "cur_from": cur_from,
            "cur_to": cur_to,
            "prev_start": prev_start,
            "prev_end_incl": prev_end_incl,
            "prev_from": prev_from,
            "prev_to": prev_to,
        }

        cur_group.setdefault((cur_from, cur_to), []).append(item)
        prev_group.setdefault((prev_from, prev_to), []).append(item)
        computed_rows.append(item)

    try:
        prev_completed_map: Dict[str, int] = {}
        for (from_d, to_d), items in prev_group.items():
            cmap = fetch_status_complete_map_cached(from_d, to_d)
            for it in items:
                prev_completed_map[it["real_key"]] = int(cmap.get(it["real_key"], 0))

        final_rows = []
        for (from_d, to_d), items in cur_group.items():
            cmap = fetch_status_complete_map_cached(from_d, to_d)
            for it in items:
                rr = it["rider"]
                nm = rr.get("name") or ""
                created_raw = rr.get("createdDate")
                created_d = created_raw[:10] if isinstance(created_raw, str) and len(created_raw) >= 10 else "-"

                cur_completed = int(cmap.get(it["real_key"], 0))
                prev_completed = int(prev_completed_map.get(it["real_key"], 0))

                planned_grade = grade_from_total(cur_completed)
                current_grade = grade_from_total(prev_completed)
                nxt, remain = next_grade_target(cur_completed)

                ov = join_overrides.get(it["login_key"])
                join_default_val = ov if ov else it["eff_join"].isoformat()

                login_badge = "가상뒷4" if it["login_src"] == "override" else "실제뒷4"
                login_badge_color = "#111" if it["login_src"] == "override" else "#888"

                join_badge = "관리자설정" if it["join_src"] == "override" else "배민입사"
                join_badge_color = "#111" if it["join_src"] == "override" else "#888"

                final_rows.append({
                    "name": nm,
                    "created": created_d,
                    "real4": it["real4"],
                    "login4": it["login4"],
                    "login_badge": login_badge,
                    "login_badge_color": login_badge_color,
                    "join_effective": it["eff_join"].isoformat(),
                    "join_badge": join_badge,
                    "join_badge_color": join_badge_color,
                    "join_default_val": join_default_val,
                    "policy_from": it["cur_start"].isoformat(),
                    "policy_to": it["cur_end_incl"].isoformat(),
                    "api_from": it["cur_from"].isoformat(),
                    "api_to": it["cur_to"].isoformat(),
                    "cur_completed": cur_completed,
                    "prev_completed": prev_completed,
                    "current_grade": current_grade,
                    "planned_grade": planned_grade,
                    "next": nxt or "-",
                    "remain": remain if remain is not None else "-",
                    "login_key": it["login_key"],
                    "name_norm": it["nn"],
                })

        final_rows.sort(key=lambda x: x["cur_completed"], reverse=True)

    except PermissionError:
        return session_expired_page()
    except RuntimeError:
        return session_expired_page()

    tr_html = ""
    for i, it in enumerate(final_rows, start=1):
        tr_html += f"""
        <tr>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#999;">{i}</td>
          <td style="padding:10px; border-bottom:1px solid #eee; font-weight:900;">{it['name']}</td>

          <td style="padding:10px; border-bottom:1px solid #eee; color:#666;">
            배민뒷4: {it['real4']}<br/>
            <b>로그인뒷4: {it['login4']}</b>
            <div style="margin-top:6px;">
              <span style="font-size:12px; color:{it['login_badge_color']}; border:1px solid #ddd; padding:2px 8px; border-radius:999px; background:#fafafa;">
                {it['login_badge']}
              </span>
            </div>

            <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
              <form method="post" action="/admin/set-login4" style="display:flex; gap:6px; align-items:center;">
                <input type="hidden" name="name_norm" value="{it['name_norm']}" />
                <input type="hidden" name="real4" value="{it['real4']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <input name="login4" value="{it['login4']}" placeholder="4자리"
                       style="width:90px; padding:8px 10px; border:1px solid #ddd; border-radius:10px;" />
                <button type="submit" style="padding:8px 10px; border:none; border-radius:10px; background:#111; color:#fff;">변경</button>
              </form>

              <form method="post" action="/admin/clear-login4">
                <input type="hidden" name="name_norm" value="{it['name_norm']}" />
                <input type="hidden" name="real4" value="{it['real4']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <button type="submit" style="padding:8px 10px; border:1px solid #ddd; border-radius:10px; background:#fff; color:#111;">초기화</button>
              </form>
            </div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; color:#666;">{it['created']}</td>

          <td style="padding:10px; border-bottom:1px solid #eee;">
            <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
              <div style="font-weight:900;">{it['join_effective']}</div>
              <span style="font-size:12px; color:{it['join_badge_color']}; border:1px solid #ddd; padding:2px 8px; border-radius:999px; background:#fafafa;">
                {it['join_badge']}
              </span>
            </div>

            <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
              <form method="post" action="/admin/set-join" style="display:flex; gap:6px; align-items:center;">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <input name="join_date" value="{it['join_default_val']}" placeholder="YYYY-MM-DD"
                       style="width:120px; padding:8px 10px; border:1px solid #ddd; border-radius:10px;" />
                <button type="submit" style="padding:8px 10px; border:none; border-radius:10px; background:#111; color:#fff;">저장</button>
              </form>

              <form method="post" action="/admin/clear-join">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <button type="submit" style="padding:8px 10px; border:1px solid #ddd; border-radius:10px; background:#fff; color:#111;">초기화</button>
              </form>
            </div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; color:#666;">
            <div style="font-weight:700;">정책: {it['policy_from']} ~ {it['policy_to']}</div>
            <div style="font-size:12px; color:#999; margin-top:4px;">API반영: {it['api_from']} ~ {it['api_to']}</div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:right; font-weight:900;">{it['cur_completed']}</td>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
            <div style="font-weight:900;">{it['current_grade']}</div>
            <div style="font-size:12px; color:#999;">({it['prev_completed']}건)</div>
          </td>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
            <div style="font-weight:900;">{it['planned_grade']}</div>
            <div style="font-size:12px; color:#999;">(현재)</div>
          </td>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center; color:#666;">{it['next']}</td>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#666;">{it['remain']}</td>
        </tr>
        """

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px;">
      <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:10px; flex-wrap:wrap;">
        <div>
          <h2 style="margin:0 0 6px 0;">전체 등급 현황</h2>
          <div style="color:#666;">
            - <b>계약종료</b> 라이더는 자동 제외<br/>
            - <b>로그인뒷4</b>는 관리자에서 변경 가능(기사들끼리 실제 번호 알아도 로그인 차단)<br/>
            - <b>현재등급</b> = 직전 평가기간 완료건수 등급<br/>
            - <b>예정등급</b> = 현재 평가기간 완료건수 등급<br/>
            - <b>다음등급/남은건수</b> = 예정등급 기준
          </div>
          <div style="color:#888; font-size:13px; margin-top:6px;">
            * 완료건수는 ‘어제까지’ 확정치입니다.
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center;">
          <a href="/" style="text-decoration:none; color:#111;">개인 조회</a>
          <a href="/admin-logout" style="text-decoration:none; color:#666;">로그아웃</a>
        </div>
      </div>

      <form method="get" action="/dashboard" style="margin-top:12px; display:flex; gap:8px;">
        <input name="q" value="{q}"
               placeholder="이름 또는 배민뒷4 검색 (예: 이정 / 1898)"
               style="flex:1; font-size:16px; padding:10px 12px; border:1px solid #ddd; border-radius:12px;" />
        <button type="submit"
                style="font-size:16px; padding:10px 14px; border:none; border-radius:12px; background:#111; color:#fff;">
          검색
        </button>
      </form>

      <div style="margin-top:14px; overflow:auto; border:1px solid #eee; border-radius:12px;">
        <table style="border-collapse:collapse; width:100%; min-width:1400px;">
          <thead>
            <tr style="background:#fafafa;">
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#999;">#</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">이름</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">로그인 설정</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">배민 입사일</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">기준일(수정가능)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">평가기간</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right;">완료(현재)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">현재등급(이전)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">예정등급(현재)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">다음등급</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right;">남은건수</th>
            </tr>
          </thead>
          <tbody>
            {tr_html if tr_html else '<tr><td colspan="11" style="padding:14px; color:#777;">조회 결과가 없습니다.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
    """
    return html_page("전체 등급 현황", body)


# -----------------------------
# Diagnostics
# -----------------------------
@app.get("/health", response_class=HTMLResponse)
def health():
    ok_cookie = bool((os.getenv("BAEMIN_COOKIE") or "").strip())
    ok_center = bool((os.getenv("BAEMIN_CENTER_ID") or "").strip())

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:520px; margin:0 auto;">
      <h3 style="margin-top:0;">Health</h3>
      <div>cookie_env_set: <b>{ok_cookie}</b></div>
      <div>center_id_env_set: <b>{ok_center}</b></div>
      <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 홈</a></div>
    </div>
    """
    return html_page("Health", body)

from fastapi.responses import PlainTextResponse

@app.get("/debug-api", response_class=PlainTextResponse)
def debug_api():
    try:
        j = api_get(f"{BASE_API}/rider", params={
            "name": "", "userId": "", "phoneNumber": "",
            "accountStatus": "", "orderName": "", "orderBy": ""
        })
        t = str(type(j))
        head = (json.dumps(j, ensure_ascii=False)[:500] if isinstance(j, (dict, list)) else str(j)[:500])
        return f"OK type={t}\nHEAD={head}\n"
    except Exception as e:
        # api_get에서 raise 된 메시지 그대로 보여주기
        return f"ERR {type(e).__name__}: {e}\n"
