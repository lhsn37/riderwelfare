from playwright.sync_api import sync_playwright
import os, json

STATE_FILE = os.getenv("BAEMIN_STATE_FILE", "storage_state.json")

print("[안내] 브라우저를 엽니다. 배민 로그인 + 인증번호 입력까지 완료하세요.")
print("[안내] 배달센터 메인 화면이 뜨면 콘솔로 돌아와 Enter를 누르세요.")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://deliverycenter.baemin.com", timeout=60000)

    input("로그인 완료했으면 Enter...")

    context.storage_state(path=STATE_FILE)
    print(f"✅ 저장 완료: {STATE_FILE}")

    browser.close()
