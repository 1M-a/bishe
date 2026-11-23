@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title Adjust System (port 5002)
REM ============================================================
REM  Adjust version: port 5002, dev.db, RESIDENT_AUTH=1, SITE_CODE=B
REM  Brand new file: do NOT edit 启动系统.bat
REM ============================================================
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "FLASK_ENV=dev"
set "APP_PORT=5002"
set "DATABASE_URL=sqlite:///%CD%\data\dev.db"
set "NOTICE_DIR=%CD%\data\notices_dev"
set "EVIDENCE_DIR=%CD%\data\evidence_dev"
set "SITE_CODE=B"
set "RESIDENT_AUTH=1"
set "SECRET_KEY=adjust-5002-7c1d9e2f3a4b5c6d7e8f90123456789a"
set "ORG_STREET=以朵街道"
set "ORG_COMMUNITY=住武社区居民委员会"
set "ORG_PROPERTY=住武物业服务有限公司"
set "ORG_PROPERTY_SHORT=住武物业"
"%CD%\venv\Scripts\python.exe" run.py 2>nul || python run.py
endlocal
