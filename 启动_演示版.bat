@echo off
setlocal
cd /d "%~dp0"
title Demo System (port 5000)

REM ============================================================
REM  Demo version  : port 5000, community.db, RESIDENT_AUTH=0
REM  Brand new file: do NOT edit 启动系统.bat
REM ============================================================
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "FLASK_ENV=dev"
set "APP_PORT=5000"
set "DATABASE_URL=sqlite:///%CD%\data\community.db"
set "NOTICE_DIR=%CD%\data\notices"
set "EVIDENCE_DIR=%CD%\data\evidence"
set "SITE_CODE=A"
set "RESIDENT_AUTH=0"
set "SECRET_KEY=demo-5000-4f8a2b1c9d3e5f6071829a3b4c5d6e70"

"%CD%\venv\Scripts\python.exe" run.py 2>nul || python run.py
endlocal