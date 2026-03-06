# main.py
# ------------------------------------------------------------------------------------
# Render 조회용: PC Collector가 업로드한 데이터만 사용
# 추가 기능:
# - 개인 조회: 오늘 완료 / 거절 / 취소 / 거절+취소율 표시
# - 관리자 대시보드: 이름 옆 거절+취소율 배지 표시
# - 이전등급 플러스 / 예정등급 플러스 엑셀 백업 다운로드
# - 기존 dashboard.xlsx 또는 plus-backup.xlsx 업로드 시 prev_plus / planned_plus 자동 복원
# ------------------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import StreamingResponse

# -----------------------------
# Config
# -----------------------------
RIDERS_CACHE_TTL = 10
STATUS_CACHE_TTL = 10
DELIVERY_STATUS_CACHE_TTL = 5

RATE_WINDOW_SEC = 60
RATE_MAX_REQ = 30

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "0315")
SESSION_SECRET = os.getenv("SESSION_SECRET", "rider-welfare-admin-secret")
INGEST_TOKEN = (os.getenv("INGEST_TOKEN") or "").strip()

OVERRIDE_FILE = "join_overrides.json"            # key: "normname|login4" -> "YYYY-MM-DD"
LOGIN4_FILE = "login4_overrides.json"            # key: "normname|real4"  -> "login4"
PREVPLUS_FILE = "prevplus_overrides.json"        # key: "normname|login4" -> int
PLANNEDPLUS_FILE = "plannedplus_overrides.json"  # key: "normname|login4" -> int
ATTENDANCE_FILE = "attendance_log.json"          # key: login_key -> {period_start, period_end, days:[YYYY-MM-DD...]}

ATTENDANCE_BONUS_PER_DAY = 5
ATTENDANCE_MIN_TODAY_COMPLETE = 6

PCX_START_DATE = date(2025, 11, 26)
PCX_LABEL = "25.11.26 ~ (어제)"

_override_lock = threading.Lock()
_login4_lock = threading.Lock()
_prevplus_lock = threading.Lock()
_plannedplus_lock = threading.Lock()
_att_lock = threading.Lock()

_rate_bucket: Dict[str, List[float]] = {}

# 저장 위치
STORE_DIR = Path(os.getenv("STORE_DIR", "."))
STORE_DIR.mkdir(parents=True, exist_ok=True)
RIDERS_STORE = STORE_DIR / "store_riders.json"
STATUS_STORE = STORE_DIR / "store_status.json"
DELIVERY_STATUS_STORE = STORE_DIR / "store_delivery_status.json"

# 메모리 캐시
_riders_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_status_cache: Dict[str, Any] = {}
_delivery_status_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

app = FastAPI(title="라웰 등급 조회 (Collector Ingest)")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# -----------------------------
# HTML helpers
# -----------------------------
def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; padding:16px; background:#fafafa;">
  <div style="max-width:1400px; margin:0 auto;">
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
    desc = st.get("desc") or ""
    if "END" in code or "TERMIN" in code or "EXPIRE" in code:
        return True
    if "계약" in desc and "종료" in desc:
        return True
    return False


# -----------------------------
# Ratio helpers
# -----------------------------
def calc_bad_ratio(complete: int, reject: int, cancel: int) -> Dict[str, Any]:
    complete = int(complete or 0)
    reject = int(reject or 0)
    cancel = int(cancel or 0)

    total = complete + reject + cancel
    bad = reject + cancel

    if total > 0:
        ratio = round((bad / total) * 100, 1)
    else:
        ratio = 0.0

    if ratio <= 19:
        fg = "#15803d"
        bg = "#e8f7ee"
        label = "양호"
    elif ratio < 30:
        fg = "#a16207"
        bg = "#fff8db"
        label = "주의"
    else:
        fg = "#b91c1c"
        bg = "#fdecec"
        label = "위험"

    return {
        "total": total,
        "bad": bad,
        "ratio": ratio,
        "fg": fg,
        "bg": bg,
        "label": label,
    }


def build_today_stats_map(ds: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    rows = ds.get("data") or []
    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = norm_name(row.get("name") or "")
        ph = (row.get("phoneNumber") or "").replace(" ", "")
        real4 = last4_from_phone(ph)
        if not nm or not real4:
            continue

        dac = row.get("deliveryAcceptanceCount") or {}
        complete = int(dac.get("complete") or 0)
        reject = int(dac.get("reject") or 0)
        cancel = int(dac.get("cancel") or 0)
        ratio_info = calc_bad_ratio(complete, reject, cancel)

        st = row.get("status") or {}
        out[f"{nm}|{real4}"] = {
            "complete": complete,
            "reject": reject,
            "cancel": cancel,
            "status_desc": st.get("desc") or "-",
            **ratio_info,
        }

    return out


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
# Period logic
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
    next_anchor = clamp_day(next_m.year, next_m.month, join_day)

    end_inclusive = next_anchor - timedelta(days=1)
    return start_d, end_inclusive


def period_to_from_to(start_d: date, end_inclusive: date) -> Tuple[date, date]:
    api_max = date.today() - timedelta(days=1)
    from_d = start_d
    to_d = min(end_inclusive, api_max)
    if from_d > to_d:
        from_d = to_d
    return from_d, to_d


# -----------------------------
# Join-date override
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
# Login4 override
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
        try:
            return date.fromisoformat(created_raw[:10]), "createdDate"
        except Exception:
            pass

    return date.today(), "fallback"


# -----------------------------
# PrevPlus override
# -----------------------------
def load_prevplus_map() -> Dict[str, int]:
    with _prevplus_lock:
        if not os.path.exists(PREVPLUS_FILE):
            return {}
        try:
            with open(PREVPLUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            out: Dict[str, int] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except Exception:
                        pass
            return out
        except Exception:
            return {}


def save_prevplus_map(data: Dict[str, int]) -> None:
    with _prevplus_lock:
        with open(PREVPLUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_prevplus(login_key: str) -> int:
    return int(load_prevplus_map().get(login_key, 0) or 0)


def set_prevplus(login_key: str, v: int) -> None:
    m = load_prevplus_map()
    m[login_key] = int(v)
    save_prevplus_map(m)


def clear_prevplus(login_key: str) -> None:
    m = load_prevplus_map()
    if login_key in m:
        del m[login_key]
    save_prevplus_map(m)


# -----------------------------
# PlannedPlus override
# -----------------------------
def load_plannedplus_map() -> Dict[str, int]:
    with _plannedplus_lock:
        if not os.path.exists(PLANNEDPLUS_FILE):
            return {}
        try:
            with open(PLANNEDPLUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            out: Dict[str, int] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except Exception:
                        pass
            return out
        except Exception:
            return {}


def save_plannedplus_map(data: Dict[str, int]) -> None:
    with _plannedplus_lock:
        with open(PLANNEDPLUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_plannedplus(login_key: str) -> int:
    return int(load_plannedplus_map().get(login_key, 0) or 0)


def set_plannedplus(login_key: str, v: int) -> None:
    m = load_plannedplus_map()
    m[login_key] = int(v)
    save_plannedplus_map(m)


def clear_plannedplus(login_key: str) -> None:
    m = load_plannedplus_map()
    if login_key in m:
        del m[login_key]
    save_plannedplus_map(m)


# -----------------------------
# Attendance
# -----------------------------
def load_attendance_map() -> Dict[str, Any]:
    with _att_lock:
        if not os.path.exists(ATTENDANCE_FILE):
            return {}
        try:
            with open(ATTENDANCE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_attendance_map(data: Dict[str, Any]) -> None:
    with _att_lock:
        with open(ATTENDANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _att_get_record(att_map: Dict[str, Any], login_key: str) -> Dict[str, Any]:
    rec = att_map.get(login_key)
    if isinstance(rec, dict):
        return rec
    return {"period_start": "", "period_end": "", "days": []}


def attendance_rollover_if_needed(login_key: str, cur_start: date, cur_end_incl: date) -> Tuple[int, int, bool]:
    att_map = load_attendance_map()
    rec = _att_get_record(att_map, login_key)

    rec_ps = safe_date_parse(rec.get("period_start", ""))
    rec_pe = safe_date_parse(rec.get("period_end", ""))

    if rec_ps is None or rec_pe is None or not rec.get("period_start"):
        rec["period_start"] = cur_start.isoformat()
        rec["period_end"] = cur_end_incl.isoformat()
        rec["days"] = list(dict.fromkeys(rec.get("days") or []))
        att_map[login_key] = rec
        save_attendance_map(att_map)
        return 0, get_prevplus(login_key), False

    if rec_ps == cur_start and rec_pe == cur_end_incl:
        return 0, get_prevplus(login_key), False

    days = rec.get("days") or []
    if not isinstance(days, list):
        days = []
    days = [d for d in days if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]

    bonus = len(set(days)) * ATTENDANCE_BONUS_PER_DAY
    if bonus:
        prevplus_now = get_prevplus(login_key)
        set_prevplus(login_key, prevplus_now + bonus)

    rec["period_start"] = cur_start.isoformat()
    rec["period_end"] = cur_end_incl.isoformat()
    rec["days"] = []
    att_map[login_key] = rec
    save_attendance_map(att_map)

    return bonus, get_prevplus(login_key), True


def attendance_is_checked_today(login_key: str, today: date, cur_start: date, cur_end_incl: date) -> bool:
    att_map = load_attendance_map()
    rec = _att_get_record(att_map, login_key)
    if rec.get("period_start") != cur_start.isoformat() or rec.get("period_end") != cur_end_incl.isoformat():
        return False
    days = rec.get("days") or []
    if not isinstance(days, list):
        return False
    return today.isoformat() in set([d for d in days if isinstance(d, str)])


def attendance_mark_today(login_key: str, today: date, cur_start: date, cur_end_incl: date) -> bool:
    att_map = load_attendance_map()
    rec = _att_get_record(att_map, login_key)

    if rec.get("period_start") != cur_start.isoformat() or rec.get("period_end") != cur_end_incl.isoformat():
        rec["period_start"] = cur_start.isoformat()
        rec["period_end"] = cur_end_incl.isoformat()
        rec["days"] = []

    days = rec.get("days") or []
    if not isinstance(days, list):
        days = []
    days_set = set([d for d in days if isinstance(d, str)])

    if today.isoformat() in days_set:
        return False

    days.append(today.isoformat())
    rec["days"] = list(dict.fromkeys(days))
    att_map[login_key] = rec
    save_attendance_map(att_map)
    return True


def attendance_count_in_period(login_key: str, cur_start: date, cur_end_incl: date) -> int:
    att_map = load_attendance_map()
    rec = _att_get_record(att_map, login_key)
    if rec.get("period_start") != cur_start.isoformat() or rec.get("period_end") != cur_end_incl.isoformat():
        return 0
    days = rec.get("days") or []
    if not isinstance(days, list):
        return 0
    return len(set([d for d in days if isinstance(d, str)]))


# -----------------------------
# Store helpers
# -----------------------------
def _read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _require_ingest(request: Request):
    token = request.headers.get("x-ingest-token", "")
    if not INGEST_TOKEN or token != INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "UNAUTHORIZED"}, status_code=401)
    return None


# -----------------------------
# Data access
# -----------------------------
def fetch_riders_cached() -> List[Dict[str, Any]]:
    now = time.time()
    if _riders_cache["data"] is not None and now - _riders_cache["ts"] <= RIDERS_CACHE_TTL:
        return _riders_cache["data"]

    store = _read_json(RIDERS_STORE, {"riders": []})
    riders = store.get("riders") or []
    if not isinstance(riders, list):
        riders = []
    riders = [r for r in riders if isinstance(r, dict)]
    riders = [r for r in riders if not is_ended_contract(r)]

    _riders_cache["ts"] = now
    _riders_cache["data"] = riders
    return riders


def fetch_status_complete_map_cached(from_d: date, to_d: date) -> Dict[str, int]:
    key = f"{from_d.isoformat()}_{to_d.isoformat()}"
    now = time.time()
    cached = _status_cache.get(key)
    if cached and now - cached["ts"] <= STATUS_CACHE_TTL:
        return cached["data"]

    all_status = _read_json(STATUS_STORE, {})
    m = all_status.get(key) or {}
    out: Dict[str, int] = {}
    if isinstance(m, dict):
        for k, v in m.items():
            try:
                out[str(k)] = int(v)
            except Exception:
                pass

    _status_cache[key] = {"ts": now, "data": out}
    return out


def has_status_range(from_d: date, to_d: date) -> bool:
    key = f"{from_d.isoformat()}_{to_d.isoformat()}"
    all_status = _read_json(STATUS_STORE, {}) or {}
    return isinstance(all_status, dict) and (key in all_status)


def fetch_delivery_status_cached() -> Dict[str, Any]:
    now = time.time()
    if _delivery_status_cache["data"] is not None and now - _delivery_status_cache["ts"] <= DELIVERY_STATUS_CACHE_TTL:
        return _delivery_status_cache["data"]

    st = _read_json(DELIVERY_STATUS_STORE, {"data": {}, "ts": 0})
    data = st.get("data") if isinstance(st, dict) else {}
    if not isinstance(data, dict):
        data = {}

    _delivery_status_cache["ts"] = now
    _delivery_status_cache["data"] = data
    return data


def store_ready() -> bool:
    st = _read_json(RIDERS_STORE, {})
    riders = st.get("riders") if isinstance(st, dict) else None
    return isinstance(riders, list) and len(riders) > 0


def not_ready_page() -> HTMLResponse:
    body = """
    <div style="background:#fff;border:1px solid #e8e8e8;border-radius:16px;padding:16px;max-width:720px;margin:0 auto;">
      <h3 style="margin-top:0;">아직 데이터가 없습니다</h3>
      <div style="color:#666;line-height:1.6;">
        Render는 배민 API를 직접 호출하지 않습니다.<br/>
        PC에서 collector.py를 실행해서 <b>/ingest</b>로 데이터를 먼저 업로드해야 조회가 됩니다.
      </div>
      <div style="margin-top:12px;">
        <a href="/health" style="text-decoration:none;color:#111;">Health 보기</a>
      </div>
    </div>
    """
    return HTMLResponse(html_page("데이터 없음", body), status_code=200)


# -----------------------------
# Admin helpers
# -----------------------------
def require_admin(request: Request) -> Optional[RedirectResponse]:
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin-login", status_code=303)
    return None


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, float) and v != v:
            return default
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


def restore_plus_from_workbook(file_bytes: bytes) -> Tuple[int, int]:
    """
    지원 컬럼:
    - name
    - login4
    - prev_plus
    - planned_plus
    기존 dashboard.xlsx / plus-backup.xlsx 둘 다 복원 가능
    """
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("업로드된 엑셀에 데이터가 없습니다.")

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    idx = {h: i for i, h in enumerate(headers)}

    required = ["name", "login4", "prev_plus", "planned_plus"]
    missing = [k for k in required if k not in idx]
    if missing:
        raise ValueError(f"필수 컬럼 없음: {', '.join(missing)}")

    prev_map: Dict[str, int] = {}
    planned_map: Dict[str, int] = {}

    restored_rows = 0
    for row in rows[1:]:
        if row is None:
            continue

        name = row[idx["name"]] if idx["name"] < len(row) else None
        login4 = row[idx["login4"]] if idx["login4"] < len(row) else None
        prev_plus = row[idx["prev_plus"]] if idx["prev_plus"] < len(row) else None
        planned_plus = row[idx["planned_plus"]] if idx["planned_plus"] < len(row) else None

        name_s = str(name).strip() if name is not None else ""
        login4_s = str(login4).strip() if login4 is not None else ""
        if not name_s or not re.fullmatch(r"\d{4}", login4_s):
            continue

        key = f"{norm_name(name_s)}|{login4_s}"
        prev_map[key] = _safe_int(prev_plus, 0)
        planned_map[key] = _safe_int(planned_plus, 0)
        restored_rows += 1

    save_prevplus_map(prev_map)
    save_plannedplus_map(planned_map)
    return restored_rows, len(prev_map)


# -----------------------------
# Ingest endpoints
# -----------------------------
@app.post("/ingest/riders")
async def ingest_riders(request: Request):
    auth = _require_ingest(request)
    if auth:
        return auth

    payload = await request.json()
    riders = payload.get("riders")
    if not isinstance(riders, list):
        return JSONResponse({"ok": False, "error": "INVALID_PAYLOAD"}, status_code=400)

    _write_json(RIDERS_STORE, {"ts": time.time(), "riders": riders})
    _riders_cache["ts"] = 0.0
    _riders_cache["data"] = None
    return {"ok": True, "count": len(riders)}


@app.post("/ingest/delivery-status")
async def ingest_delivery_status(request: Request):
    auth = _require_ingest(request)
    if auth:
        return auth

    payload = await request.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "INVALID_PAYLOAD"}, status_code=400)

    _write_json(DELIVERY_STATUS_STORE, {"ts": time.time(), "data": data})
    _delivery_status_cache["ts"] = 0.0
    _delivery_status_cache["data"] = None
    return {"ok": True}


@app.get("/ingest/ranges")
def ingest_ranges(request: Request):
    auth = _require_ingest(request)
    if auth:
        return auth

    store = _read_json(RIDERS_STORE, {"riders": []})
    riders = store.get("riders") or []
    if not isinstance(riders, list) or not riders:
        return {"ok": False, "error": "NO_RIDERS"}

    today = date.today()
    ranges = set()

    for rr in riders:
        if not isinstance(rr, dict):
            continue
        ph = rr.get("phoneNumber") or ""
        real4 = last4_from_phone(ph)
        if not real4:
            continue

        login4, _, _ = get_login4_for_rider(rr)
        eff_join, _ = get_effective_join_date_by_login_key(rr, login4)

        cur_start, cur_end_incl = current_period(eff_join, today)
        cur_from, cur_to = period_to_from_to(cur_start, cur_end_incl)

        prev_end_incl = cur_start - timedelta(days=1)
        prev_m = date(cur_start.year, cur_start.month, 1) + relativedelta(months=-1)
        prev_start = clamp_day(prev_m.year, prev_m.month, eff_join.day)
        prev_from, prev_to = period_to_from_to(prev_start, prev_end_incl)

        ranges.add((cur_from.isoformat(), cur_to.isoformat()))
        ranges.add((prev_from.isoformat(), prev_to.isoformat()))

    out = [{"fromDate": a, "toDate": b} for (a, b) in sorted(ranges)]
    return {"ok": True, "ranges": out, "count": len(out)}


@app.post("/ingest/status")
async def ingest_status(request: Request):
    auth = _require_ingest(request)
    if auth:
        return auth

    payload = await request.json()
    from_d = payload.get("fromDate")
    to_d = payload.get("toDate")
    complete_map = payload.get("completeMap")

    if not (isinstance(from_d, str) and isinstance(to_d, str) and isinstance(complete_map, dict)):
        return JSONResponse({"ok": False, "error": "INVALID_PAYLOAD"}, status_code=400)

    all_status = _read_json(STATUS_STORE, {})
    key = f"{from_d}_{to_d}"
    all_status[key] = complete_map
    _write_json(STATUS_STORE, all_status)

    _status_cache.pop(key, None)
    return {"ok": True, "key": key, "count": len(complete_map)}


# -----------------------------
# Main routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    if not store_ready():
        return not_ready_page()

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
        * 완료건수는 ‘어제까지’ 반영됩니다. (등급용)<br/>
        * 출석체크는 ‘오늘 완료건수’ 기준입니다. (실시간)
      </div>
    </div>
    """
    return html_page("라웰 등급 조회", body)


@app.get("/check")
def check_get_redirect():
    return RedirectResponse(url="/", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
def admin_help():
    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:900px; margin:0 auto;">
      <h2 style="margin:0 0 6px 0;">관리자</h2>
      <div style="color:#666; margin-bottom:12px;">
        이 서버(Render)는 배민 API를 직접 호출하지 않습니다.<br/>
        PC에서 collector.py가 /ingest 로 데이터를 업로드하면 조회가 됩니다.
      </div>

      <div style="background:#f7f7f7; border-radius:12px; padding:12px; font-size:14px; line-height:1.6;">
        <div><b>Render 설정</b></div>
        <div>- Settings → Environment → <b>INGEST_TOKEN</b> 등록</div>
        <div>- Start Command: <span style="font-family: ui-monospace;">uvicorn main:app --host 0.0.0.0 --port $PORT</span></div>
      </div>

      <div style="margin-top:14px; background:#fff8e8; border:1px solid #ffe1a8; border-radius:12px; padding:12px; font-size:14px; line-height:1.6;">
        <div><b>PCX 누적 집계</b></div>
        <div>- 개인조회 화면에 <b>{PCX_LABEL}</b> 누적 완료건수를 보여줍니다.</div>
        <div>- 단, PC에서 그 기간({PCX_START_DATE.isoformat()} ~ 어제) 범위를 <b>한 번이라도</b> 업로드해야 숫자가 뜹니다.</div>
      </div>

      <div style="margin-top:14px; background:#eef6ff; border:1px solid #cfe3ff; border-radius:12px; padding:12px; font-size:14px; line-height:1.6;">
        <div><b>플러스 백업 / 복원</b></div>
        <div>- 관리자 대시보드에서 <b>플러스 백업 엑셀</b> 다운로드 가능</div>
        <div>- 코드 업데이트 후 기존 <b>dashboard.xlsx</b> 또는 <b>plus-backup.xlsx</b> 업로드 시</div>
        <div>&nbsp;&nbsp;→ <b>이전등급 플러스(prev_plus)</b>, <b>예정등급 플러스(planned_plus)</b> 자동 복원</div>
      </div>

      <div style="margin-top:14px;">
        <a href="/" style="text-decoration:none; color:#111;">← 조회 화면으로</a>
      </div>
    </div>
    """
    return html_page("관리자", body)


@app.post("/attendance-check")
def attendance_check(request: Request, name: str = Form(...), login4: str = Form(...)):
    if not store_ready():
        return not_ready_page()

    ip = request.client.host if request.client else "unknown"
    if not rate_limit(ip):
        return RedirectResponse("/", status_code=303)

    name_in = norm_name(name)
    login4 = (login4 or "").strip()

    if not re.fullmatch(r"\d{4}", login4):
        return RedirectResponse("/", status_code=303)

    riders = fetch_riders_cached()
    candidates = [r for r in riders if norm_name(r.get("name", "")) == name_in]
    matches: List[Dict[str, Any]] = []
    for r in candidates:
        l4, _, _ = get_login4_for_rider(r)
        if l4 == login4:
            matches.append(r)

    if len(matches) != 1:
        return RedirectResponse("/", status_code=303)

    rider = matches[0]
    rider_login4, rider_real4, _ = get_login4_for_rider(rider)
    eff_join_date, _ = get_effective_join_date_by_login_key(rider, rider_login4)

    today = date.today()
    cur_start, cur_end_incl = current_period(eff_join_date, today)
    login_key = f"{name_in}|{rider_login4}"

    attendance_rollover_if_needed(login_key, cur_start, cur_end_incl)

    ds = fetch_delivery_status_cached()
    stats_map = build_today_stats_map(ds)
    today_completed = int((stats_map.get(f"{name_in}|{rider_real4}") or {}).get("complete") or 0)

    if today_completed < ATTENDANCE_MIN_TODAY_COMPLETE:
        return RedirectResponse("/", status_code=303)

    if attendance_is_checked_today(login_key, today, cur_start, cur_end_incl):
        return RedirectResponse("/", status_code=303)

    ok = attendance_mark_today(login_key, today, cur_start, cur_end_incl)
    if not ok:
        return RedirectResponse("/", status_code=303)

    planned_plus = get_plannedplus(login_key)
    set_plannedplus(login_key, planned_plus + ATTENDANCE_BONUS_PER_DAY)

    return RedirectResponse("/", status_code=303)


@app.post("/check", response_class=HTMLResponse)
def check(request: Request, name: str = Form(...), login4: str = Form(...)):
    if not store_ready():
        return not_ready_page()

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

    riders = fetch_riders_cached()
    candidates = [r for r in riders if norm_name(r.get("name", "")) == name_in]
    matches: List[Dict[str, Any]] = []
    for r in candidates:
        l4, _, _ = get_login4_for_rider(r)
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
    rider_login4, rider_real4, _ = get_login4_for_rider(rider)
    eff_join_date, join_src = get_effective_join_date_by_login_key(rider, rider_login4)

    today = date.today()
    cur_start, cur_end_incl = current_period(eff_join_date, today)
    cur_from, cur_to = period_to_from_to(cur_start, cur_end_incl)

    prev_end_incl = cur_start - timedelta(days=1)
    prev_m = date(cur_start.year, cur_start.month, 1) + relativedelta(months=-1)
    prev_start = clamp_day(prev_m.year, prev_m.month, eff_join_date.day)
    prev_from, prev_to = period_to_from_to(prev_start, prev_end_incl)

    cmap_cur = fetch_status_complete_map_cached(cur_from, cur_to)
    cmap_prev = fetch_status_complete_map_cached(prev_from, prev_to)

    api_key = f"{name_in}|{rider_real4}"
    cur_completed_raw = int(cmap_cur.get(api_key, 0))
    prev_completed_raw = int(cmap_prev.get(api_key, 0))

    login_key = f"{name_in}|{rider_login4}"
    prev_plus = get_prevplus(login_key)
    planned_plus = get_plannedplus(login_key)

    rolled_bonus, prevplus_after, rolled = attendance_rollover_if_needed(login_key, cur_start, cur_end_incl)
    if rolled:
        prev_plus = prevplus_after

    ds = fetch_delivery_status_cached()
    today_stats_map = build_today_stats_map(ds)
    today_info = today_stats_map.get(api_key) or {
        "complete": 0,
        "reject": 0,
        "cancel": 0,
        "status_desc": "-",
        **calc_bad_ratio(0, 0, 0),
    }

    today_completed = int(today_info["complete"])
    today_rejected = int(today_info["reject"])
    today_canceled = int(today_info["cancel"])
    today_status_desc = str(today_info["status_desc"])
    today_total_attempt = int(today_info["total"])
    today_bad_ratio = float(today_info["ratio"])
    ratio_bg = str(today_info["bg"])
    ratio_fg = str(today_info["fg"])
    ratio_label = str(today_info["label"])

    att_count = attendance_count_in_period(login_key, cur_start, cur_end_incl)
    att_checked_today = attendance_is_checked_today(login_key, today, cur_start, cur_end_incl)
    attendance_enabled = (today_completed >= ATTENDANCE_MIN_TODAY_COMPLETE) and (not att_checked_today)

    planned_total = cur_completed_raw + planned_plus
    prev_total = prev_completed_raw + prev_plus

    planned_grade = grade_from_total(planned_total)
    current_grade = grade_from_total(prev_total)

    # 다음등급/남은건수도 예정등급 계산 기준과 동일하게
    nxt, remain = next_grade_target(planned_total)

    join_note = "관리자 설정" if join_src == "override" else "배민 입사일"

    pcx_from = PCX_START_DATE
    pcx_to = date.today() - timedelta(days=1)
    pcx_text = "데이터 없음(업로드 필요)"
    if pcx_from <= pcx_to and has_status_range(pcx_from, pcx_to):
        cmap_pcx = fetch_status_complete_map_cached(pcx_from, pcx_to)
        pcx_text = f"{int(cmap_pcx.get(api_key, 0))}건"

    rollover_note = ""
    if rolled_bonus:
        rollover_note = f"<div style='margin-top:8px;color:#111;font-size:13px;'><b>출석 이관:</b> 이전기간 출석보너스 {rolled_bonus}건이 현재등급(이전) 플러스에 자동 합산되었습니다.</div>"

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:920px; margin:0 auto;">
      <h2 style="margin:0 0 6px 0;">등급 조회 결과</h2>

      <div style="color:#888; font-size:13px; margin-top:6px;">
        기준일(입사일): <b>{eff_join_date}</b> ({join_note})
      </div>

      <div style="margin-top:12px; padding:12px; border:1px solid #eee; border-radius:14px; background:#fcfcfc;">
        <div style="font-size:18px;"><b>{rider.get('name','')}</b> 님</div>
        <div style="color:#777; margin-top:6px;">휴대폰: {mask_phone(rider.get('phoneNumber',''))}</div>
      </div>

      <div style="display:flex; gap:12px; margin-top:12px; flex-wrap:wrap;">
        <div style="flex:1; min-width:250px; padding:12px; border-radius:12px; border:1px solid #eee; background:#fff;">
          <div style="color:#777; font-size:13px;">현재등급(이전기간)</div>
          <div style="font-size:32px; font-weight:900; line-height:1.1;">{current_grade}</div>
          <div style="font-size:12px; color:#999; margin-top:6px;">
            정책기간: {prev_start} ~ {prev_end_incl}<br/>
            반영기간(업로드): {prev_from} ~ {prev_to}<br/>
            완료 {prev_completed_raw}건 + 이전등급플러스 {prev_plus}건 = <b>{prev_total}</b>건
          </div>
        </div>

        <div style="flex:1; min-width:250px; padding:12px; border-radius:12px; border:1px solid #eee; background:#fff;">
          <div style="color:#777; font-size:13px;">예정등급(현재기간)</div>
          <div style="font-size:32px; font-weight:900; line-height:1.1;">{planned_grade}</div>
          <div style="font-size:12px; color:#999; margin-top:6px;">
            정책기간: {cur_start} ~ {cur_end_incl}<br/>
            반영기간(업로드): {cur_from} ~ {cur_to}<br/>
            완료 {cur_completed_raw}건 + 예정등급플러스 {planned_plus}건 = <b>{planned_total}</b>건
          </div>
        </div>
      </div>

      <div style="margin-top:12px; padding:12px; border:1px solid #eee; border-radius:14px; background:#fff;">
        <div style="display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap;">
          <div style="color:#666;">
            다음등급: <b>{(nxt or '-')}</b> / 남은건수: <b>{(remain if remain is not None else '-')}</b>
            <div style="color:#999; font-size:12px; margin-top:6px;">* 다음등급/남은건수는 “현재기간 완료건수(어제까지)” 기준입니다.</div>
          </div>

          <div style="color:#111; font-weight:900; min-width:300px;">
            <div>오늘완료(실시간): {today_completed}건</div>
            <div style="margin-top:4px; font-size:14px; font-weight:700; color:#666;">
              거절: {today_rejected}건 / 취소: {today_canceled}건
            </div>
            <div style="margin-top:8px;">
              <span style="
                display:inline-block;
                padding:6px 10px;
                border-radius:999px;
                background:{ratio_bg};
                color:{ratio_fg};
                font-size:13px;
                font-weight:900;
                border:1px solid rgba(0,0,0,0.06);
              ">
                거절+취소율 {today_bad_ratio}% ({ratio_label})
              </span>
            </div>
            <div style="color:#777; font-weight:600; font-size:12px; margin-top:6px;">
              운행기준: 완료 {today_completed} + 거절 {today_rejected} + 취소 {today_canceled} = 총 {today_total_attempt}건
            </div>
            <div style="color:#777; font-weight:600; font-size:12px; margin-top:4px;">운행상태: {today_status_desc}</div>
          </div>
        </div>

        <div style="margin-top:10px; border-top:1px dashed #eee; padding-top:10px;">
          <div style="font-weight:900; margin-bottom:6px;">출석체크</div>
          <div style="color:#666; font-size:13px; line-height:1.6;">
            - 조건: <b>오늘 완료 {ATTENDANCE_MIN_TODAY_COMPLETE}건 이상</b>이면 출석체크 가능 (운행중/운행종료 무관)<br/>
            - 출석 1회당 <b>+{ATTENDANCE_BONUS_PER_DAY}건</b> (예정등급에 즉시 반영)<br/>
            - 기간 종료 시 출석보너스는 <b>현재등급(이전) 플러스</b>로 자동 이관 후 초기화
          </div>

          <div style="margin-top:10px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <div style="color:#111; font-weight:900;">이번기간 출석: {att_count}회 (보너스 {att_count * ATTENDANCE_BONUS_PER_DAY}건)</div>
            <form method="post" action="/attendance-check" style="margin:0;">
              <input type="hidden" name="name" value="{name}" />
              <input type="hidden" name="login4" value="{login4}" />
              <button type="submit"
                {'disabled' if not attendance_enabled else ''}
                style="padding:10px 14px; border:none; border-radius:12px; background:{'#111' if attendance_enabled else '#bbb'}; color:#fff; font-weight:900; cursor:{'pointer' if attendance_enabled else 'not-allowed'};">
                {'오늘 출석체크 완료' if att_checked_today else '출석체크 (+5)'}
              </button>
            </form>
            <div style="color:#999; font-size:12px;">
              {'' if attendance_enabled else ('오늘완료가 부족합니다' if today_completed < ATTENDANCE_MIN_TODAY_COMPLETE else '이미 오늘 출석체크 완료')}
            </div>
          </div>
          {rollover_note}
        </div>
      </div>

      <div style="margin-top:12px; padding:12px; border:1px solid #eee; border-radius:14px; background:#fff;">
        <div style="font-weight:900; margin-bottom:6px;">PCX 이벤트 누적 완료건수</div>
        <div style="color:#666;">기간: <b>{PCX_LABEL}</b></div>
        <div style="font-size:22px; font-weight:900; margin-top:6px;">{pcx_text}</div>
        <div style="color:#999; font-size:12px; margin-top:6px;">
          * 이 숫자는 PC에서 <b>{PCX_START_DATE.isoformat()} ~ 어제</b> 범위를 업로드했을 때만 표시됩니다.
        </div>
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
# Admin set / clear
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


@app.post("/admin/set-prevplus")
def admin_set_prevplus(request: Request, key: str = Form(...), prevplus: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    v = _safe_int(prevplus, 0)
    if not key:
        return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)

    v = max(-999, min(999, v))
    set_prevplus(key, v)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/clear-prevplus")
def admin_clear_prevplus(request: Request, key: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    if key:
        clear_prevplus(key)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/set-plannedplus")
def admin_set_plannedplus(request: Request, key: str = Form(...), plannedplus: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    v = _safe_int(plannedplus, 0)
    if not key:
        return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)

    v = max(-999, min(999, v))
    set_plannedplus(key, v)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


@app.post("/admin/clear-plannedplus")
def admin_clear_plannedplus(request: Request, key: str = Form(...), redirect_q: str = Form(default="")):
    r = require_admin(request)
    if r:
        return r

    key = (key or "").strip()
    if key:
        clear_plannedplus(key)
    return RedirectResponse(f"/dashboard?q={redirect_q}", status_code=303)


# -----------------------------
# Plus backup / restore
# -----------------------------
@app.get("/plus-backup.xlsx")
def plus_backup_excel(request: Request):
    r = require_admin(request)
    if r:
        return r

    if not store_ready():
        return not_ready_page()

    riders = fetch_riders_cached()
    prevplus_map = load_prevplus_map()
    plannedplus_map = load_plannedplus_map()

    rows = []
    for rr in riders:
        name = rr.get("name") or ""
        if not name:
            continue
        login4, real4, _ = get_login4_for_rider(rr)
        key = f"{norm_name(name)}|{login4}"
        rows.append({
            "name": name,
            "login4": login4,
            "real4": real4,
            "prev_plus": int(prevplus_map.get(key, 0) or 0),
            "planned_plus": int(plannedplus_map.get(key, 0) or 0),
        })

    rows.sort(key=lambda x: x["name"])

    wb = Workbook()
    ws = wb.active
    ws.title = "plus_backup"

    headers = ["name", "login4", "real4", "prev_plus", "planned_plus"]
    ws.append(headers)
    for r0 in rows:
        ws.append([r0.get(h, "") for h in headers])

    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 4)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = f"riderwelfare_plus_backup_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/restore-plus-xlsx")
async def admin_restore_plus_xlsx(request: Request, file: UploadFile = File(...)):
    r = require_admin(request)
    if r:
        return r

    if not file.filename.lower().endswith(".xlsx"):
        body = """
        <div style="max-width:640px; margin:40px auto; background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:20px;">
          <h3 style="margin-top:0;">업로드 실패</h3>
          <div style="color:#666;">xlsx 파일만 업로드할 수 있습니다.</div>
          <div style="margin-top:12px;"><a href="/dashboard" style="text-decoration:none; color:#111;">← 대시보드로 돌아가기</a></div>
        </div>
        """
        return HTMLResponse(html_page("업로드 실패", body))

    try:
        file_bytes = await file.read()
        restored_rows, saved_count = restore_plus_from_workbook(file_bytes)
        body = f"""
        <div style="max-width:720px; margin:40px auto; background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:20px;">
          <h3 style="margin-top:0;">플러스 복원 완료</h3>
          <div style="color:#666; line-height:1.7;">
            업로드 파일: <b>{file.filename}</b><br/>
            처리 행 수: <b>{restored_rows}</b><br/>
            저장된 키 수: <b>{saved_count}</b><br/>
            이전등급 플러스 / 예정등급 플러스가 모두 갱신되었습니다.
          </div>
          <div style="margin-top:14px;"><a href="/dashboard" style="text-decoration:none; color:#111;">← 대시보드로 돌아가기</a></div>
        </div>
        """
        return HTMLResponse(html_page("플러스 복원 완료", body))
    except Exception as e:
        body = f"""
        <div style="max-width:720px; margin:40px auto; background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:20px;">
          <h3 style="margin-top:0;">플러스 복원 실패</h3>
          <div style="color:#666; line-height:1.7;">
            파일: <b>{file.filename}</b><br/>
            오류: <b>{str(e)}</b><br/>
            업로드 엑셀에 <b>name / login4 / prev_plus / planned_plus</b> 컬럼이 있어야 합니다.
          </div>
          <div style="margin-top:14px;"><a href="/dashboard" style="text-decoration:none; color:#111;">← 대시보드로 돌아가기</a></div>
        </div>
        """
        return HTMLResponse(html_page("플러스 복원 실패", body))


# -----------------------------
# Dashboard
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, q: str = ""):
    r = require_admin(request)
    if r:
        return r

    if not store_ready():
        return not_ready_page()

    riders = fetch_riders_cached()
    join_overrides = load_overrides()
    prevplus_map = load_prevplus_map()
    plannedplus_map = load_plannedplus_map()
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
    ds = fetch_delivery_status_cached()
    today_stats_map = build_today_stats_map(ds)

    cur_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}
    prev_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}

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
        prev_m = date(cur_start.year, cur_start.month, 1) + relativedelta(months=-1)
        prev_start = clamp_day(prev_m.year, prev_m.month, eff_join.day)
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

            cur_completed_raw = int(cmap.get(it["real_key"], 0))
            prev_completed_raw = int(prev_completed_map.get(it["real_key"], 0))

            prev_plus = int(prevplus_map.get(it["login_key"], 0) or 0)
            planned_plus = int(plannedplus_map.get(it["login_key"], 0) or 0)

            planned_total = cur_completed_raw + planned_plus
            prev_total = prev_completed_raw + prev_plus

            planned_grade = grade_from_total(planned_total)
            current_grade = grade_from_total(prev_total)

            nxt, remain = next_grade_target(cur_completed_raw)

            ov = join_overrides.get(it["login_key"])
            join_default_val = ov if ov else it["eff_join"].isoformat()

            login_badge = "가상뒷4" if it["login_src"] == "override" else "실제뒷4"
            login_badge_color = "#111" if it["login_src"] == "override" else "#888"

            join_badge = "관리자설정" if it["join_src"] == "override" else "배민입사"
            join_badge_color = "#111" if it["join_src"] == "override" else "#888"

            today_info = today_stats_map.get(it["real_key"]) or {
                "complete": 0,
                "reject": 0,
                "cancel": 0,
                "status_desc": "-",
                **calc_bad_ratio(0, 0, 0),
            }

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
                "cur_completed_raw": cur_completed_raw,
                "prev_completed_raw": prev_completed_raw,
                "prev_plus": prev_plus,
                "planned_plus": planned_plus,
                "current_grade": current_grade,
                "planned_grade": planned_grade,
                "next": nxt or "-",
                "remain": remain if remain is not None else "-",
                "login_key": it["login_key"],
                "name_norm": it["nn"],

                "today_complete": int(today_info["complete"]),
                "today_reject": int(today_info["reject"]),
                "today_cancel": int(today_info["cancel"]),
                "today_total": int(today_info["total"]),
                "today_ratio": float(today_info["ratio"]),
                "today_ratio_fg": str(today_info["fg"]),
                "today_ratio_bg": str(today_info["bg"]),
                "today_ratio_label": str(today_info["label"]),
                "today_status_desc": str(today_info["status_desc"]),
            })

    final_rows.sort(key=lambda x: (x["cur_completed_raw"], -x["today_ratio"]), reverse=True)

    tr_html = ""
    for i, it in enumerate(final_rows, start=1):
        tr_html += f"""
        <tr>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#999;">{i}</td>

          <td style="padding:10px; border-bottom:1px solid #eee;">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              <div style="font-weight:900;">{it['name']}</div>
              <span style="
                display:inline-block;
                padding:3px 8px;
                border-radius:999px;
                background:{it['today_ratio_bg']};
                color:{it['today_ratio_fg']};
                font-size:12px;
                font-weight:900;
                border:1px solid rgba(0,0,0,0.06);
              ">
                거절+취소 {it['today_ratio']}%
              </span>
            </div>
            <div style="font-size:12px; color:#999; margin-top:4px;">
              오늘 완료 {it['today_complete']} / 거절 {it['today_reject']} / 취소 {it['today_cancel']}
            </div>
          </td>

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
            <div style="font-size:12px; color:#999; margin-top:4px;">업로드 반영: {it['api_from']} ~ {it['api_to']}</div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
            <div style="font-weight:900;">{it['today_complete']}</div>
            <div style="font-size:12px; color:#999;">거절 {it['today_reject']} / 취소 {it['today_cancel']}</div>
            <div style="font-size:12px; color:{it['today_ratio_fg']}; font-weight:900; margin-top:4px;">{it['today_ratio']}%</div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
              <div style="font-weight:900; font-size:20px;">{it['cur_completed_raw'] + it['planned_plus']}</div>
              <div style="font-size:12px; color:#999; margin-top:4px;">
                  현재 {it['cur_completed_raw']} + 플러스 {it['planned_plus']}
              </div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
            <div style="font-weight:900;">{it['current_grade']}</div>
            <div style="font-size:12px; color:#999;">이전 {it['prev_completed_raw']} + 플러스 {it['prev_plus']}</div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center;">
            <div style="font-weight:900;">{it['planned_grade']}</div>
            <div style="font-size:12px; color:#999;">현재 {it['cur_completed_raw']} + 플러스 {it['planned_plus']}</div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; text-align:center; color:#666;">{it['next']}</td>
          <td style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#666;">{it['remain']}</td>

          <td style="padding:10px; border-bottom:1px solid #eee; color:#666; min-width:260px;">
            <div style="font-weight:700;">현재등급(이전) 플러스</div>
            <div style="font-size:12px; color:#999;">이전등급 계산에만 적용</div>
            <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
              <form method="post" action="/admin/set-prevplus" style="display:flex; gap:6px; align-items:center;">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <input name="prevplus" value="{it['prev_plus']}" placeholder="예: 20"
                       style="width:90px; padding:8px 10px; border:1px solid #ddd; border-radius:10px;" />
                <button type="submit" style="padding:8px 10px; border:none; border-radius:10px; background:#111; color:#fff;">저장</button>
              </form>

              <form method="post" action="/admin/clear-prevplus">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <button type="submit" style="padding:8px 10px; border:1px solid #ddd; border-radius:10px; background:#fff; color:#111;">초기화</button>
              </form>
            </div>
          </td>

          <td style="padding:10px; border-bottom:1px solid #eee; color:#666; min-width:260px;">
            <div style="font-weight:700;">예정등급(현재) 플러스</div>
            <div style="font-size:12px; color:#999;">예정등급 계산에만 적용</div>
            <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
              <form method="post" action="/admin/set-plannedplus" style="display:flex; gap:6px; align-items:center;">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <input name="plannedplus" value="{it['planned_plus']}" placeholder="예: 20"
                       style="width:90px; padding:8px 10px; border:1px solid #ddd; border-radius:10px;" />
                <button type="submit" style="padding:8px 10px; border:none; border-radius:10px; background:#111; color:#fff;">저장</button>
              </form>

              <form method="post" action="/admin/clear-plannedplus">
                <input type="hidden" name="key" value="{it['login_key']}" />
                <input type="hidden" name="redirect_q" value="{q}" />
                <button type="submit" style="padding:8px 10px; border:1px solid #ddd; border-radius:10px; background:#fff; color:#111;">초기화</button>
              </form>
            </div>
          </td>
        </tr>
        """

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px;">
      <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:10px; flex-wrap:wrap;">
        <div>
          <h2 style="margin:0 0 6px 0;">전체 등급 현황</h2>
          <div style="color:#666; line-height:1.6;">
            - <b>계약종료</b> 라이더는 자동 제외<br/>
            - <b>오늘운행</b>은 실시간 기준: 완료 / 거절 / 취소 / 거절+취소율 표시<br/>
            - <b>현재등급(이전)</b>은 “이전기간 완료건수 + 플러스(개인별)”로 계산<br/>
            - <b>예정등급(현재)</b>은 “현재기간 완료건수 + 예정플러스(개인별)”로 계산
          </div>
          <div style="color:#888; font-size:13px; margin-top:6px;">
            * 이름 옆 배지는 <b>거절+취소율</b>입니다. 19% 이하 초록 / 20~29.9% 노랑 / 30% 이상 빨강
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <a href="/dashboard.xlsx" style="text-decoration:none; color:#111;">전체 엑셀 다운로드</a>
          <a href="/plus-backup.xlsx" style="text-decoration:none; color:#111;">플러스 백업 엑셀</a>
          <a href="/" style="text-decoration:none; color:#111;">개인 조회</a>
          <a href="/admin-logout" style="text-decoration:none; color:#666;">로그아웃</a>
        </div>
      </div>

      <div style="margin-top:14px; display:flex; gap:12px; flex-wrap:wrap;">
        <form method="get" action="/dashboard" style="display:flex; gap:8px; flex:1 1 480px;">
          <input name="q" value="{q}"
                 placeholder="이름 또는 배민뒷4 검색 (예: 이정 / 1898)"
                 style="flex:1; font-size:16px; padding:10px 12px; border:1px solid #ddd; border-radius:12px;" />
          <button type="submit"
                  style="font-size:16px; padding:10px 14px; border:none; border-radius:12px; background:#111; color:#fff;">
            검색
          </button>
        </form>

        <form method="post" action="/admin/restore-plus-xlsx" enctype="multipart/form-data"
              style="display:flex; gap:8px; align-items:center; flex:1 1 420px; background:#f7f9fc; padding:10px 12px; border:1px solid #e4ebf5; border-radius:12px;">
          <input type="file" name="file" accept=".xlsx"
                 style="flex:1; font-size:14px;" required />
          <button type="submit"
                  style="font-size:14px; padding:10px 14px; border:none; border-radius:12px; background:#111; color:#fff;">
            플러스 복원 업로드
          </button>
        </form>
      </div>

      <div style="margin-top:8px; color:#777; font-size:13px;">
        * 기존 <b>dashboard.xlsx</b> 또는 <b>플러스 백업 엑셀</b> 업로드 가능 (필수 컬럼: name / login4 / prev_plus / planned_plus)
      </div>

      <div style="margin-top:14px; overflow:auto; border:1px solid #eee; border-radius:12px;">
        <table style="border-collapse:collapse; width:100%; min-width:2350px;">
          <thead>
            <tr style="background:#fafafa;">
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right; color:#999;">#</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">이름 / 오늘비율</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">로그인 설정</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">배민 입사일</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">기준일(수정가능)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">평가기간</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">오늘운행</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right;">완료(현재)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">현재등급(이전)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">예정등급(현재)</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:center;">다음등급</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:right;">남은건수</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">이전등급 플러스</th>
              <th style="padding:10px; border-bottom:1px solid #eee; text-align:left;">예정등급 플러스</th>
            </tr>
          </thead>
          <tbody>
            {tr_html if tr_html else '<tr><td colspan="14" style="padding:14px; color:#777;">조회 결과가 없습니다.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
    """
    return html_page("전체 등급 현황", body)


# -----------------------------
# Excel export
# -----------------------------
@app.get("/dashboard.xlsx")
def dashboard_excel(request: Request):
    r = require_admin(request)
    if r:
        return r

    if not store_ready():
        return not_ready_page()

    riders = fetch_riders_cached()
    prevplus_map = load_prevplus_map()
    plannedplus_map = load_plannedplus_map()
    today_stats_map = build_today_stats_map(fetch_delivery_status_cached())

    today = date.today()
    cur_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}
    prev_group: Dict[Tuple[date, date], List[Dict[str, Any]]] = {}

    for rr in riders:
        nm = rr.get("name") or ""
        ph = rr.get("phoneNumber") or ""
        real4 = last4_from_phone(ph)
        if not real4:
            continue

        nn = norm_name(nm)
        login4, _, _ = get_login4_for_rider(rr)
        real_key = f"{nn}|{real4}"
        login_key = f"{nn}|{login4}"

        eff_join, _ = get_effective_join_date_by_login_key(rr, login4)

        cur_start, cur_end_incl = current_period(eff_join, today)
        cur_from, cur_to = period_to_from_to(cur_start, cur_end_incl)

        prev_end_incl = cur_start - timedelta(days=1)
        prev_m = date(cur_start.year, cur_start.month, 1) + relativedelta(months=-1)
        prev_start = clamp_day(prev_m.year, prev_m.month, eff_join.day)
        prev_from, prev_to = period_to_from_to(prev_start, prev_end_incl)

        it = {
            "name": nm,
            "login4": login4,
            "real4": real4,
            "real_key": real_key,
            "login_key": login_key,
            "join_effective": eff_join.isoformat(),
            "policy_from": cur_start.isoformat(),
            "policy_to": cur_end_incl.isoformat(),
            "api_from": cur_from.isoformat(),
            "api_to": cur_to.isoformat(),
            "prev_api_from": prev_from.isoformat(),
            "prev_api_to": prev_to.isoformat(),
            "prev_plus": int(prevplus_map.get(login_key, 0) or 0),
            "planned_plus": int(plannedplus_map.get(login_key, 0) or 0),
        }

        cur_group.setdefault((cur_from, cur_to), []).append(it)
        prev_group.setdefault((prev_from, prev_to), []).append(it)

    prev_completed_map: Dict[str, int] = {}
    for (from_d, to_d), items in prev_group.items():
        cmap = fetch_status_complete_map_cached(from_d, to_d)
        for it in items:
            prev_completed_map[it["real_key"]] = int(cmap.get(it["real_key"], 0))

    rows = []
    for (from_d, to_d), items in cur_group.items():
        cmap = fetch_status_complete_map_cached(from_d, to_d)
        for it in items:
            cur_raw = int(cmap.get(it["real_key"], 0))
            prev_raw = int(prev_completed_map.get(it["real_key"], 0))
            prev_plus = int(it["prev_plus"])
            planned_plus = int(it["planned_plus"])

            prev_total_for_grade = prev_raw + prev_plus
            planned_total_for_grade = cur_raw + planned_plus

            planned_grade = grade_from_total(planned_total_for_grade)
            current_grade = grade_from_total(prev_total_for_grade)
            nxt, remain = next_grade_target(cur_raw)

            today_info = today_stats_map.get(it["real_key"]) or {
                "complete": 0,
                "reject": 0,
                "cancel": 0,
                **calc_bad_ratio(0, 0, 0),
            }

            rows.append({
                "name": it["name"],
                "login4": it["login4"],
                "real4": it["real4"],
                "join_effective": it["join_effective"],
                "policy_from": it["policy_from"],
                "policy_to": it["policy_to"],
                "api_from": it["api_from"],
                "api_to": it["api_to"],
                "today_complete": int(today_info["complete"]),
                "today_reject": int(today_info["reject"]),
                "today_cancel": int(today_info["cancel"]),
                "today_ratio": float(today_info["ratio"]),
                "cur_completed": cur_raw,
                "prev_plus": prev_plus,
                "planned_plus": planned_plus,
                "planned_total_for_grade": planned_total_for_grade,
                "planned_grade_cur": planned_grade,
                "prev_completed": prev_raw,
                "prev_total_for_grade": prev_total_for_grade,
                "current_grade_prev": current_grade,
                "next": nxt or "",
                "remain": remain if remain is not None else "",
            })

    rows.sort(key=lambda x: x["cur_completed"], reverse=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "dashboard"

    headers = [
        "name", "login4", "real4", "join_effective",
        "policy_from", "policy_to", "api_from", "api_to",
        "today_complete", "today_reject", "today_cancel", "today_ratio",
        "cur_completed",
        "prev_plus", "planned_plus",
        "planned_total_for_grade", "planned_grade_cur",
        "prev_completed", "prev_total_for_grade", "current_grade_prev",
        "next", "remain",
    ]
    ws.append(headers)
    for r0 in rows:
        ws.append([r0.get(h, "") for h in headers])

    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, min(28, len(h) + 4))

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = f"riderwelfare_dashboard_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------------
# Diagnostics
# -----------------------------
@app.get("/health", response_class=HTMLResponse)
def health():
    ok_token = bool(INGEST_TOKEN)
    riders_ok = store_ready()
    riders_ts = _read_json(RIDERS_STORE, {}).get("ts")
    status_keys = list((_read_json(STATUS_STORE, {}) or {}).keys())[:5]
    delivery_ts = _read_json(DELIVERY_STATUS_STORE, {}).get("ts")

    pcx_from = PCX_START_DATE
    pcx_to = date.today() - timedelta(days=1)
    pcx_ready = (pcx_from <= pcx_to) and has_status_range(pcx_from, pcx_to)

    body = f"""
    <div style="background:#fff; border:1px solid #e8e8e8; border-radius:16px; padding:16px; max-width:820px; margin:0 auto;">
      <h3 style="margin-top:0;">Health</h3>
      <div>ingest_token_set: <b>{ok_token}</b></div>
      <div>riders_uploaded: <b>{riders_ok}</b></div>
      <div>riders_ts: <b>{riders_ts}</b></div>
      <div>delivery_status_ts: <b>{delivery_ts}</b></div>
      <div style="margin-top:8px;">status_keys(sample): <b>{status_keys}</b></div>
      <div style="margin-top:8px;">pcx_range_ready({PCX_START_DATE.isoformat()}~어제): <b>{pcx_ready}</b></div>
      <div style="margin-top:12px;"><a href="/" style="text-decoration:none; color:#111;">← 홈</a></div>
    </div>
    """
    return html_page("Health", body)


@app.get("/version")
def version():
    return {
        "ok": True,
        "ts": int(time.time()),
        "render_git": os.getenv("RENDER_GIT_COMMIT", ""),
    }