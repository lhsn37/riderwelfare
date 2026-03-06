from dotenv import load_dotenv
load_dotenv()

import os
import time
import json
import requests
from datetime import date, timedelta
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

RENDER_BASE = os.getenv("RENDER_BASE", "https://riderwelfare.onrender.com").strip().strip('"')
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()
CENTER_ID = os.getenv("BAEMIN_CENTER_ID", "").strip()
STATE_FILE = os.getenv("BAEMIN_STATE_FILE", "storage_state.json").strip().strip('"')
CUM_START_DATE = os.getenv("CUM_START_DATE", "2025-11-26").strip()

# 기본은 브라우저 띄우기(권장)
HEADLESS = os.getenv("HEADLESS", "0").strip() not in ("0", "false", "False", "")

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


def _browser_like_headers():
    # 너가 Network에서 확인한 것과 최대한 유사
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ko-KR,ko;q=0.9",
        "center-id": CENTER_ID,
    }


def _fetch_json_in_page(page, url: str, headers: dict, timeout_ms: int = 30000):
    """
    ✅ 핵심: 브라우저 페이지 컨텍스트에서 fetch() 실행
    - credentials: 'include' 로 쿠키 포함
    - Cloudflare는 브라우저 요청은 통과시키는 경우가 많음
    """
    js = """
    async ({ url, headers, timeoutMs }) => {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), timeoutMs);

      try {
        const res = await fetch(url, {
          method: "GET",
          headers,
          credentials: "include",
          signal: ctrl.signal,
        });

        const ct = res.headers.get("content-type") || "";
        const text = await res.text();

        // JSON이면 파싱
        if (ct.includes("application/json")) {
          return { ok: res.ok, status: res.status, ct, json: JSON.parse(text), head: "" };
        }

        // HTML(Cloudflare) 등
        return { ok: res.ok, status: res.status, ct, json: null, head: text.slice(0, 800) };
      } catch (e) {
        return { ok: false, status: 0, ct: "", json: null, head: String(e) };
      } finally {
        clearTimeout(t);
      }
    }
    """
    out = page.evaluate(js, {"url": url, "headers": headers, "timeoutMs": timeout_ms})
    if not out.get("ok") or out.get("json") is None:
        raise RuntimeError(f"FETCH_HTTP_{out.get('status')} CT={out.get('ct')} HEAD={out.get('head')}")
    return out["json"]


def fetch_riders(page):
    params = {
        "name": "",
        "userId": "",
        "phoneNumber": "",
        "accountStatus": "",
        "orderName": "",
        "orderBy": "",
    }
    url = f"{BASE_API}/rider?{urlencode(params)}"
    return _fetch_json_in_page(page, url, _browser_like_headers())


def fetch_delivery_status(page):
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
    url = f"{BASE_API}/management/delivery-status?{urlencode(params)}"
    return _fetch_json_in_page(page, url, _browser_like_headers())


def fetch_status_range(page, fromDate: str, toDate: str):
    complete = {}
    page_no = 0
    size = 100

    while True:
        params = {"page": page_no, "size": size, "fromDate": fromDate, "toDate": toDate}
        url = f"{BASE_API}/management/rider-delivery-status?{urlencode(params)}"
        j = _fetch_json_in_page(page, url, _browser_like_headers())
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

        page_no += 1
        if page_no > 800:
            break

    return complete


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
        raise SystemExit(f"{STATE_FILE} 파일이 없습니다. 먼저 login_once.py로 storage_state.json을 생성하세요.")

    print(f"[collector] HEADLESS={HEADLESS}")
    print(f"[collector] CENTER_ID={CENTER_ID}")
    print(f"[collector] RENDER_BASE={RENDER_BASE}")

    while True:
        try:
            with sync_playwright() as p:
                # ✅ 실제 브라우저 컨텍스트 사용
                browser = p.chromium.launch(
                    headless=HEADLESS,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    storage_state=STATE_FILE,
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                )
                page = context.new_page()

                # ✅ 반드시 deliverycenter.baemin.com 페이지를 먼저 열어야 CORS/쿠키가 정상
                page.goto("https://deliverycenter.baemin.com/", wait_until="domcontentloaded")
                page.wait_for_timeout(1200)

                # 1) riders
                riders_j = fetch_riders(page)
                riders_list = normalize_riders(riders_j)
                post_json("/ingest/riders", {"riders": riders_list})

                # 1.5) delivery-status
                ds_j = fetch_delivery_status(page)
                post_json("/ingest/delivery-status", {"data": ds_j})

                # 2) ranges
                ranges_resp = get_ranges()
                ranges = list(ranges_resp.get("ranges") or []) if ranges_resp.get("ok") else []

                # 2.5) 누적 range
                cum_from = CUM_START_DATE
                cum_to = (date.today() - timedelta(days=1)).isoformat()
                if cum_from <= cum_to:
                    if not any(r.get("fromDate") == cum_from and r.get("toDate") == cum_to for r in ranges):
                        ranges.append({"fromDate": cum_from, "toDate": cum_to})

                # 3) status 수집
                for rg in ranges:
                    fd = rg["fromDate"]
                    td = rg["toDate"]
                    cm = fetch_status_range(page, fd, td)
                    post_json("/ingest/status", {"fromDate": fd, "toDate": td, "completeMap": cm})

                context.close()
                browser.close()

            print("OK sync", time.strftime("%Y-%m-%d %H:%M:%S"))

        except Exception as e:
            print("ERR", type(e).__name__, e)

        time.sleep(60)


if __name__ == "__main__":
    main_loop()