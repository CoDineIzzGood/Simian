@echo off
setlocal
REM Launch Simian API from the new directory path
cd /d "D:\Project_C.H.I.M.P\Simian"

call venv\Scripts\activate

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"

REM Optional hardened env pulled from .env or pre-set in your shell
if not defined SIMIAN_ALLOWED_ORIGINS set "SIMIAN_ALLOWED_ORIGINS=http://127.0.0.1:*,http://localhost:*"
if not defined SIMIAN_ALLOW_NETS set "SIMIAN_ALLOW_NETS=127.0.0.1/32,::1/128"

python -m uvicorn main:app --host %APP_HOST% --port %APP_PORT%
endlocal
