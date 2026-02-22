import os
from playwright.sync_api import sync_playwright

STATE_FILE = os.getenv("BAEMIN_STATE_FILE", "storage_state.json")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 창 띄움
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto("https://deliverycenter.baemin.com/", wait_until="domcontentloaded")

        print("\n[안내] 지금 브라우저 창에서 배민 로그인 + 인증번호 입력까지 완료하세요.")
        print("[안내] 로그인 완료 후 '배달센터 메인 화면'이 뜨면 콘솔로 돌아와 Enter 누르세요.\n")

        input("로그인 완료했으면 Enter... ")

        # 로그인 상태 저장
        ctx.storage_state(path=STATE_FILE)
        print(f"\n✅ 저장 완료: {STATE_FILE}")

        browser.close()

if __name__ == "__main__":
    main()
