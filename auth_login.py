import json
import os
from playwright.sync_api import sync_playwright

COOKIE_FILE = "session.json"
TEST_URL = "https://api-deliverycenter.baemin.com/rider?name=&userId=&phoneNumber=&accountStatus=&orderName=&orderBy="

def login_and_save_verified():
    center_id = (os.getenv("BAEMIN_CENTER_ID") or "").strip()
    if not center_id:
        raise RuntimeError('BAEMIN_CENTER_ID 환경변수가 없습니다. setx BAEMIN_CENTER_ID "..." 로 설정하세요.')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://deliverycenter.baemin.com", wait_until="domcontentloaded")

        print("✅ 팝업에서 로그인 후, '라이더 조회' 화면까지 들어가세요.")
        print("   예) https://deliverycenter.baemin.com/rider/info")
        print("✅ 화면 진입 후 Enter를 누르면, API 인증 테스트 후 저장합니다.")
        input()

        # ✅ API 인증 테스트 (Center-Id/Origin/Referer 포함)
        resp = page.request.get(
            TEST_URL,
            headers={
                "Accept": "application/json",
                "Center-Id": center_id,
                "Origin": "https://deliverycenter.baemin.com",
                "Referer": "https://deliverycenter.baemin.com/",
            },
        )
        print("API TEST STATUS:", resp.status)

        if resp.status != 200:
            print("❌ API 인증 실패. (대부분 Center-Id 값/로그인 상태 문제)")
            browser.close()
            return

        cookies = context.cookies()
        cookies = [c for c in cookies if "baemin.com" in (c.get("domain") or "")]

        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        browser.close()
        print("✅ session.json 저장 완료! (API 인증 확인됨)")

if __name__ == "__main__":
    login_and_save_verified()
