@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title Trial System (port 5001)
REM ============================================================
REM  Trial version : port 5001, trial.db, RESIDENT_AUTH=1, SITE_CODE=C
REM  Brand new file: do NOT edit 启动系统.bat
REM  Multi-site   : copy this file, bump port to 5101/5102..., SITE_CODE=C1/C2...
REM ============================================================
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "FLASK_ENV=dev"
set "APP_PORT=5001"
set "DATABASE_URL=sqlite:///%CD%\data\trial.db"
set "NOTICE_DIR=%CD%\data\notices_trial"
set "EVIDENCE_DIR=%CD%\data\evidence_trial"
set "SITE_CODE=C"
set "RESIDENT_AUTH=1"
set "SECRET_KEY=trial-5001-1a2b3c4d5e6f708192a3b4c5d6e7f8091"
set "ORG_STREET=以朵街道"
set "ORG_COMMUNITY=住武社区居民委员会"
set "ORG_PROPERTY=住武物业服务有限公司"
set "ORG_PROPERTY_SHORT=住武物业"
"%CD%\venv\Scripts\python.exe" run.py 2>nul || python run.py
endlocal
