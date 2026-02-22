@echo off
chcp 65001 >nul
title RiderWelfare Collector
cd /d C:\rider-grade

echo ==========================================
echo Rider Welfare Collector 시작
echo ==========================================

REM 1) 패키지 설치(처음 1회 또는 에러날 때만)
python -m pip install -r requirements.txt

REM 2) (선택) 크롬 설치가 안돼있으면 playwright 크롬 설치
python -m playwright install chromium

REM 3) 로그인 상태 저장 (storage_state.json 갱신)
echo.
echo [1/2] 배민 로그인 세션 저장 시작 (login_once.py)
python login_once.py
if errorlevel 1 (
  echo [에러] login_once.py 실패
  pause
  exit /b 1
)

REM 4) 수집/업로드 시작
echo.
echo [2/2] 데이터 수집 시작 (collector.py)
python collector.py

pause
