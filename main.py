# main.py (Render / Python 3.13 / requests-only 경량화 버전)

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

# =============================
# Config
# =============================
BASE_API = "https://api-deliverycenter.baemin.com"
COOKIE_FILE = "session.json"
ADMIN_PASSWORD = "0315"

RIDERS_CACHE_TTL = 60
STATUS_CACHE_TTL = 30

app = FastAPI(title="라웰 등급 조회")
app.add_middleware(SessionMiddleware, secret_key="rider-welfare-admin-secret")

_riders_cache = {"ts": 0.0, "data": None}
_status_cache: Dict[str, Any] = {}

# =============================
# Helpers
# =============================
def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
</head>
<body style="font-family:system-ui;padding:16px;background:#fafafa">
<div style="max-width:1200px;margin:0 auto">{body}</div>
</body>
</html>"""

def norm_name(x: str) -> str:
    return re.sub(r"\s+", "", (x or "")).lower()

def last4(phone: str) -> str:
    m = re.search(r"(\d{4})$", phone or "")
    return m.group(1) if m else ""

def mask_phone(p: str) -> str:
    m = re.search(r"(\d{3})-?\d{3,4}-?(\d{4})", p or "")
    return f"{m.group(1)}-****-{m.group(2)}" if m else p

# =============================
# Auth / Headers
# =============================
def load_cookie_header() -> str:
    if os.path.exists(COOKIE_FILE):
        cookies = json.load(open(COOKIE_FILE, encoding="utf-8"))
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
    env = os.getenv("BAEMIN_COOKIE")
    if env:
        return env
    raise RuntimeError("쿠키 없음")

def headers() -> Dict[str, str]:
    cid = os.getenv("BAEMIN_CENTER_ID")
    if not cid:
        raise RuntimeError("BAEMIN_CENTER_ID 없음")
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Cookie": load_cookie_header(),
        "Center-Id": cid,
        "Origin": "https://deliverycenter.baemin.com",
        "Referer": "https://deliverycenter.baemin.com/",
    }

def api_get(url: str, params: dict = None) -> Any:
    r = requests.get(url, headers=headers(), params=params, timeout=20)
    if r.status_code in (401, 403):
        raise PermissionError("SESSION_EXPIRED")
    r.raise_for_status()
    return r.json()

# =============================
# Grade Logic
# =============================
def grade_from_total(t: int) -> str:
    if t <= 479: return "무등급"
    if t <= 719: return "R5"
    if t <= 959: return "R4"
    if t <= 1199: return "R3"
    if t <= 1439: return "R2"
    return "R1"

# =============================
# Data Fetch
# =============================
def fetch_riders() -> List[Dict]:
    now = time.time()
    if _riders_cache["data"] and now - _riders_cache["ts"] < RIDERS_CACHE_TTL:
        return _riders_cache["data"]

    j = api_get(f"{BASE_API}/rider", {
        "name": "", "userId": "", "phoneNumber": "", "accountStatus": ""
    })
    items = j.get("items", j if isinstance(j, list) else [])
    _riders_cache.update(ts=now, data=items)
    return items

def fetch_completed(from_d: date, to_d: date) -> Dict[str, int]:
    key = f"{from_d}_{to_d}"
    now = time.time()
    if key in _status_cache and now - _status_cache[key]["ts"] < STATUS_CACHE_TTL:
        return _status_cache[key]["data"]

    res = {}
    page = 0
    while True:
        j = api_get(f"{BASE_API}/management/rider-delivery-status", {
            "page": page, "size": 100,
            "fromDate": from_d.isoformat(),
            "toDate": to_d.isoformat(),
        })
        rows = j.get("data", [])
        if not rows:
            break
        for r in rows:
            k = f"{norm_name(r['name'])}|{last4(r['phoneNumber'])}"
            res[k] = r["deliveryAcceptanceCount"]["complete"]
        page += 1

    _status_cache[key] = {"ts": now, "data": res}
    return res

# =============================
# Routes
# =============================
@app.get("/", response_class=HTMLResponse)
def home():
    return html_page("라웰 등급 조회", """
    <h2>라웰 등급 조회</h2>
    <form method="post" action="/check">
      <input name="name" placeholder="이름" required><br><br>
      <input name="login4" placeholder="뒷4자리" pattern="\\d{4}" required><br><br>
      <button>조회</button>
    </form>
    """)

@app.post("/check", response_class=HTMLResponse)
def check(name: str = Form(...), login4: str = Form(...)):
    riders = fetch_riders()
    key = f"{norm_name(name)}|{login4}"

    rider = next((r for r in riders if f"{norm_name(r['name'])}|{last4(r['phoneNumber'])}" == key), None)
    if not rider:
        return html_page("없음", "<h3>조회 결과 없음</h3><a href='/'>뒤로</a>")

    today = date.today()
    start = today.replace(day=1)
    end = today - timedelta(days=1)

    cmap = fetch_completed(start, end)
    completed = cmap.get(key, 0)

    body = f"""
    <h2>{rider['name']} 님</h2>
    <div>휴대폰: {mask_phone(rider['phoneNumber'])}</div>
    <div>완료건수: <b>{completed}</b></div>
    <div>등급: <b>{grade_from_total(completed)}</b></div>
    <a href="/">다시 조회</a>
    """
    return html_page("결과", body)

@app.get("/health")
def health():
    return {"ok": True}
