from dotenv import load_dotenv
load_dotenv()

import os
import time
import json
import requests
from datetime import date, timedelta, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

RENDER_BASE = os.getenv("RENDER_BASE", "https://riderwelfare.onrender.com").strip().strip('"')
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()
CENTER_ID = os.getenv("BAEMIN_CENTER_ID", "").strip()
STATE_FILE = os.getenv("BAEMIN_STATE_FILE", "storage_state.json").strip().strip('"')
CUM_START_DATE = os.getenv("CUM_START_DATE", "2025-11-26").strip()
HISTORY_PAGE_DELAY_SEC = float(os.getenv("HISTORY_PAGE_DELAY_SEC", "0.35"))
FETCH_RETRY_COUNT = max(1, int(os.getenv("FETCH_RETRY_COUNT", "3")))
SYNC_INTERVAL_SEC = max(60, int(os.getenv("SYNC_INTERVAL_SEC", "60")))
# 팀 과거기록은 연결 안정성을 위해 기본 1일씩만 저장합니다.
HISTORY_DAY_DELAY_SEC = max(1.0, float(os.getenv("HISTORY_DAY_DELAY_SEC", "5")))

# 기본은 브라우저 띄우기(권장)
HEADLESS = os.getenv("HEADLESS", "0").strip() not in ("0", "false", "False", "")

BASE_API = "https://api-deliverycenter.baemin.com"
KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    return datetime.now(KST).date()


def now_kst_text() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def to_int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def get_total_complete(acc: dict) -> int:
    """배민 v4 완료건수: 푸드 + 비마트 + 배민스토어 전체 완료."""
    if not isinstance(acc, dict):
        return 0

    total = to_int(acc.get("totalComplete"))
    if total:
        return total

    # 혹시 totalComplete가 비어있는 응답 대비
    sum_total = (
        to_int(acc.get("foodComplete"))
        + to_int(acc.get("bmartComplete"))
        + to_int(acc.get("storeComplete"))
    )
    if sum_total:
        return sum_total

    # 구형 응답 대비
    return to_int(acc.get("complete"))


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
    """브라우저 페이지 컨텍스트에서 fetch하고 일시 오류는 재시도합니다."""
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
        if (ct.includes("application/json")) {
          try {
            return { ok: res.ok, status: res.status, ct, json: JSON.parse(text), head: "" };
          } catch (e) {
            return { ok: false, status: res.status, ct, json: null, head: text.slice(0, 800) };
          }
        }
        return { ok: res.ok, status: res.status, ct, json: null, head: text.slice(0, 800) };
      } catch (e) {
        return { ok: false, status: 0, ct: "", json: null, head: String(e) };
      } finally {
        clearTimeout(t);
      }
    }
    """

    last_error = None
    for attempt in range(1, FETCH_RETRY_COUNT + 1):
        try:
            result = page.evaluate(js, {"url": url, "headers": headers, "timeoutMs": timeout_ms})
            if result.get("ok") and result.get("json") is not None:
                return result["json"]
            last_error = RuntimeError(
                f"FETCH_HTTP_{result.get('status')} CT={result.get('ct')} HEAD={result.get('head')}"
            )
        except Exception as exc:
            last_error = exc

        if attempt < FETCH_RETRY_COUNT:
            wait_sec = min(5.0, 0.8 * attempt)
            print(f"[collector] fetch retry {attempt}/{FETCH_RETRY_COUNT}: {url}")
            page.wait_for_timeout(int(wait_sec * 1000))

    raise RuntimeError(str(last_error or "FETCH_FAILED"))


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
    url = f"{BASE_API}/v4/management/delivery-status?{urlencode(params)}"
    return _fetch_json_in_page(page, url, _browser_like_headers())


def fetch_status_range(page, fromDate: str, toDate: str):
    """과거 날짜의 기사별 전체 기록을 수집합니다.

    completeMap은 기존 등급 조회 호환용이고,
    historyMap은 팀 과거조회용 완료/거절/취소/구간/시간대 기록입니다.
    """
    complete_map = {}
    history_map = {}
    page_no = 0
    size = 100

    while True:
        params = {"page": page_no, "size": size, "fromDate": fromDate, "toDate": toDate}
        url = f"{BASE_API}/v4/management/rider-delivery-status?{urlencode(params)}"
        j = _fetch_json_in_page(page, url, _browser_like_headers())
        rows = j.get("data") or []

        for it in rows:
            nm = (it.get("name") or "").replace(" ", "").lower()
            ph = (it.get("phoneNumber") or "").replace(" ", "")
            real4 = ph[-4:] if len(ph) >= 4 else ""
            if not nm or not real4:
                continue

            key = f"{nm}|{real4}"
            acc = it.get("deliveryAcceptanceCount") or {}
            peak = it.get("deliveryPeakTimeCount") or {}
            hourly = it.get("hourlyCompleted") or []

            complete = get_total_complete(acc)
            # 관제와 동일 기준: 거절은 푸드만, 취소는 전체
            reject = to_int(acc.get("foodReject") if "foodReject" in acc else acc.get("totalReject"))
            cancel = to_int(acc.get("totalCancel"))
            if "totalCancel" not in acc:
                cancel = (
                    to_int(acc.get("foodCancel"))
                    + to_int(acc.get("bmartCancel"))
                    + to_int(acc.get("storeCancel"))
                )

            complete_map[key] = int(complete)
            history_map[key] = {
                "name": it.get("name") or "",
                "real4": real4,
                "complete": int(complete),
                "reject": int(reject),
                "cancel": int(cancel),
                "peak": {
                    "morning": to_int(peak.get("morning")),
                    "afternoon": to_int(peak.get("afternoon")),
                    "evening": to_int(peak.get("evening")),
                    "midnight": to_int(peak.get("midnight")),
                },
                "hourlyCompleted": [
                    {"hour": to_int(x.get("hour")), "count": to_int(x.get("count"))}
                    for x in hourly if isinstance(x, dict)
                ],
            }

        total_page = to_int(j.get("totalPage"))
        page_no += 1
        if total_page > 0:
            if page_no >= total_page:
                break
        elif not rows or len(rows) < size:
            break
        if page_no > 800:
            break

        # 과거 API를 연속으로 너무 빠르게 호출하지 않습니다.
        if HISTORY_PAGE_DELAY_SEC > 0:
            page.wait_for_timeout(int(HISTORY_PAGE_DELAY_SEC * 1000))

    return complete_map, history_map


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

                # 2.5) 과거 범위는 Render가 "없는 범위"만 내려줍니다.
                # collector에서 임의로 누적 범위를 매번 추가하지 않습니다.
                unique_ranges = []
                seen_ranges = set()
                for rg in ranges:
                    fd = str(rg.get("fromDate") or "")
                    td = str(rg.get("toDate") or "")
                    key = f"{fd}_{td}"
                    if not fd or not td or key in seen_ranges:
                        continue
                    seen_ranges.add(key)
                    unique_ranges.append({
                        "fromDate": fd, "toDate": td,
                        "kind": str(rg.get("kind") or "status"),
                    })

                if unique_ranges:
                    print(f"[collector] missing history ranges={len(unique_ranges)}")

                # 3) 누락된 과거 status만 수집
                for rg in unique_ranges:
                    fd = rg["fromDate"]
                    td = rg["toDate"]
                    kind = str(rg.get("kind") or "status")
                    cm, hm = fetch_status_range(page, fd, td)
                    post_json(
                        "/ingest/status",
                        {
                            "fromDate": fd,
                            "toDate": td,
                            "kind": kind,
                            "completeMap": cm,
                            "historyMap": hm,
                        },
                    )
                    print(f"[collector] {kind} saved {fd} ~ {td} riders={len(hm)}")
                    if kind == "team_history" and HISTORY_DAY_DELAY_SEC > 0:
                        page.wait_for_timeout(int(HISTORY_DAY_DELAY_SEC * 1000))

                context.close()
                browser.close()

            print("OK sync", now_kst_text())

        except Exception as e:
            print("ERR", type(e).__name__, e)

        time.sleep(SYNC_INTERVAL_SEC)


if __name__ == "__main__":
    main_loop()