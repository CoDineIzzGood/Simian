@echo off
setlocal
cd /d "D:\Project_C.H.I.M.P\Simian"

python -m venv venv
call venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

REM ffmpeg path
set "FFMPEG_DIR=%CD%\ffmpeg-7.1.1\bin"
set "PATH=%FFMPEG_DIR%;%PATH%"

REM Hardened API defaults (customize if desired)
set "SIMIAN_ALLOWED_ORIGINS=http://127.0.0.1:*,http://localhost:*"
set "SIMIAN_ALLOW_NETS=127.0.0.1/32,::1/128"
set "SIMIAN_RPS=5"
set "SIMIAN_BURST=10"

echo Ready. Use run_api.bat to start the API.
endlocal
