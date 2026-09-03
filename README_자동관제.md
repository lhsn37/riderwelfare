# 라이더웰페어 서버+로컬 자동관제 1.0

기존 `rw_monitor.py`의 관제 계산과 메시지 형식은 유지하면서 사람이 프로그램을 누르고 Enter를 입력하던 절차를 자동화한 버전입니다.

## 고정 운영 조건

- 카카오톡 채팅방: `.env`의 `KAKAO_ROOM_TITLE` 한 곳
- 운영시간: 매일 09:00~23:59
- 전송간격: 19분
- 관제 PC: Windows 로그인 및 화면 켜짐 상태

## 동작 구조

1. Render `riderwelfare`가 현재 전송 차례인지 판단합니다.
2. 로컬 `auto_agent.py`가 15초마다 서버 명령을 확인합니다.
3. 전송 차례이면 기존 ChromeProfile로 배민 관제 API를 조회합니다.
4. 기존 형식의 관제 메시지를 만들고 지정한 카카오톡 채팅방 창을 찾아 전송합니다.
5. 성공·실패·로그인 필요 상태를 Render 화면에 기록합니다.
6. 같은 시간대 작업은 로컬 상태 파일로 중복 전송하지 않습니다.

## 중요한 보안사항

기존 압축에 포함된 `.env`와 `ChromeProfile`에는 인증정보가 있습니다. 절대로 GitHub나 Render에 올리지 마세요. 새 `.gitignore`가 이를 제외합니다.

## Render 기존 riderwelfare 서비스 교체

Git 저장소에 아래 항목을 올립니다.

- `control_server/`
- `render.yaml`

기존 `riderwelfare` 서비스 설정:

- Build Command: `pip install -r control_server/requirements.txt`
- Start Command: `uvicorn control_server.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`
- 환경변수 `ADMIN_PASSWORD`: 웹 관리화면 비밀번호
- 환경변수 `CONTROL_TOKEN`: 32자 이상의 임의 문자열

`CONTROL_TOKEN`은 Render와 로컬 `.env`에 똑같이 입력합니다.

## 로컬 설치

1. 기존 폴더를 별도로 백업합니다.
2. 새 배포 ZIP의 로컬 실행 파일을 기존 `rider_monitor` 폴더에 덮어씁니다.
3. 기존 `.env`는 지우거나 덮어쓰지 말고 아래 세 줄을 추가합니다.
   - `CONTROL_SERVER_URL=https://riderwelfare.onrender.com`
   - `CONTROL_TOKEN=Render와_동일한_문자열`
   - `KAKAO_ROOM_TITLE=라이더웰페어 단톡`
4. 카카오톡에서 대상 채팅방을 별도 창으로 열어 둡니다.
5. PowerShell에서 `install_auto_agent.ps1`을 실행합니다.
6. `run_auto_agent.bat`으로 첫 테스트를 합니다.

배민 로그인이 유지된 경우 자동으로 관제가 시작됩니다. 로그인이 풀렸다면 자동으로 열린 Chrome에서 로그인과 OTP만 완료하면 Enter 입력 없이 다음 명령부터 정상 작동합니다.

## Render 관리화면

`https://riderwelfare.onrender.com`에 접속하고 사용자명 `admin`, 비밀번호는 Render의 `ADMIN_PASSWORD`를 입력합니다.

- 지금 즉시 전송
- 자동관제 일시정지/다시 시작
- 로컬 PC 마지막 연결시간
- 최근 성공시간과 오류 확인

## 기존 방식 중단

새 실행이 확인된 후 기존 `start_monitor.bat`, `start_all.bat`, `kakao_sender.exe`는 동시에 실행하지 마세요. 두 프로그램이 같이 실행되면 메시지가 중복 전송될 수 있습니다.
