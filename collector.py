from dotenv import load_dotenv
load_dotenv()

import os
import time
import requests
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

RENDER_BASE = os.getenv("RENDER_BASE", "https://riderwelfare.onrender.com").strip().strip('"')
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()
CENTER_ID = os.getenv("BAEMIN_CENTER_ID", "").strip()
STATE_FILE = os.getenv("BAEMIN_STATE_FILE", "storage_state.json").strip().strip('"')
CUM_START_DATE = os.getenv("CUM_START_DATE", "2025-11-26").strip()

BASE_API = "https://api-deliverycenter.baemin.com"

def render_headers():
    return {"x-ingest-token": INGEST_TOKEN}

def post_json(path: str, payload: dict):
    url = f"{RENDER_BASE}{path}"
    r = requests.post(url, headers=render_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def get_ranges():
    url = f"{RENDER_BASE}/ingest/ranges"
    r = requests.get(url, headers=render_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def _pw_headers():
    return {
        "accept": "application/json, text/plain, */*",
        "origin": "https://deliverycenter.baemin.com",
        "referer": "https://deliverycenter.baemin.com/",
        "user-agent": os.getenv(
            "BAEMIN_UA",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        ),
        "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "accept-encoding": "gzip, deflate, br",
        "center-id": CENTER_ID,
    }

def fetch_riders_via_playwright(context):
    url = f"{BASE_API}/rider"
    params = {"name":"","userId":"","phoneNumber":"","accountStatus":"","orderName":"","orderBy":""}
    resp = context.request.get(url, params=params, headers=_pw_headers(), timeout=30_000)
    status = resp.status
    ct = resp.headers.get("content-type","")
    if status >= 400:
        head = resp.text()[:800]
        raise RuntimeError(f"RIDERS_HTTP_{status} CT={ct} HEAD={head}")
    return resp.json()

def fetch_status_range_via_playwright(context, fromDate: str, toDate: str):
    complete = {}
    page = 0
    size = 100

    while True:
        url = f"{BASE_API}/management/rider-delivery-status"
        params = {"page": page, "size": size, "fromDate": fromDate, "toDate": toDate}
        resp = context.request.get(url, params=params, headers=_pw_headers(), timeout=30_000)

        status = resp.status
        ct = resp.headers.get("content-type","")
        if status >= 400:
            head = resp.text()[:800]
            raise RuntimeError(f"STATUS_HTTP_{status} CT={ct} HEAD={head}")

        j = resp.json()
        rows = (j.get("data") or [])
        if not rows:
            break

        for it in rows:
            nm = (it.get("name") or "").replace(" ", "").lower()
            ph = (it.get("phoneNumber") or "").replace(" ", "")
            real4 = ph[-4:] if len(ph) >= 4 else ""
            k = f"{nm}|{real4}"
            cnt = ((it.get("deliveryAcceptanceCount") or {}).get("complete")) or 0
            if real4:
                complete[k] = int(cnt)

        page += 1
        if page > 800:
            break

    return complete

def fetch_delivery_status_via_playwright(context):
    """
    ✅ 실시간 배달현황(금일): 운행상태/금일완료/거절/취소 등
    """
    url = f"{BASE_API}/management/delivery-status"
    params = {
        "page": 0,
        "size": 100,
        "orderName": "riderStatus",
        "orderBy": "asc",
        "name": "",
        "userId": "",
        "phoneNumber": "",
        "riderStatus": "",
    }
    resp = context.request.get(url, params=params, headers=_pw_headers(), timeout=30_000)
    status = resp.status
    ct = resp.headers.get("content-type","")
    if status >= 400:
        head = resp.text()[:800]
        raise RuntimeError(f"DELIVERY_STATUS_HTTP_{status} CT={ct} HEAD={head}")
    return resp.json()

def normalize_riders(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        return j.get("items") or j.get("data") or []
    return []

def main_loop():
    if not INGEST_TOKEN:
        raise SystemExit("INGEST_TOKEN env가 필요합니다.")
    if not CENTER_ID:
        raise SystemExit("BAEMIN_CENTER_ID env가 필요합니다(PC 수집용).")
    if not os.path.exists(STATE_FILE):
        raise SystemExit(f"{STATE_FILE} 파일이 없습니다. 먼저 로그인 후 storage_state.json을 생성하세요.")

    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(storage_state=STATE_FILE)

                # 1) riders
                riders_j = fetch_riders_via_playwright(context)
                riders_list = normalize_riders(riders_j)
                post_json("/ingest/riders", {"riders": riders_list})

                # 1.5) delivery-status (실시간 배달현황)
                ds_j = fetch_delivery_status_via_playwright(context)
                post_json("/ingest/delivery-status", {"data": ds_j})

                # 2) ranges
                ranges_resp = get_ranges()
                if ranges_resp.get("ok"):
                    ranges = list(ranges_resp.get("ranges") or [])
                else:
                    ranges = []

                # ✅ 전체 누적 range 추가 (시작일 ~ 어제)
                cum_from = CUM_START_DATE
                cum_to = (date.today() - timedelta(days=1)).isoformat()

                if cum_from <= cum_to:
                    if not any(r.get("fromDate") == cum_from and r.get("toDate") == cum_to for r in ranges):
                        ranges.append({"fromDate": cum_from, "toDate": cum_to})

                # 3) status 수집
                for rg in ranges:
                    fd = rg["fromDate"]
                    td = rg["toDate"]
                    cm = fetch_status_range_via_playwright(context, fd, td)
                    post_json("/ingest/status", {
                        "fromDate": fd,
                        "toDate": td,
                        "completeMap": cm
                    })

                context.close()
                browser.close()

            print("OK sync", time.strftime("%Y-%m-%d %H:%M:%S"))

        except Exception as e:
            print("ERR", type(e).__name__, e)

        time.sleep(60)

if __name__ == "__main__":
    main_loop()