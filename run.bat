@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 실행 환경이 없습니다. 설치 안내대로 먼저 설치해 주세요.
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 브라우저가 열립니다. 끝내려면 이 창을 닫으세요.
".venv\Scripts\python.exe" -m streamlit run app.py
pause
