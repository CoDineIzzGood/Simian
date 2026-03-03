@echo off
setlocal ENABLEDELAYEDEXPANSION
pushd "%~dp0"
if not exist "logs" mkdir "logs"
for /f "tokens=1-3 delims=/.- " %%a in ("%date%") do ( set _Y=%%c & set _M=%%a & set _D=%%b )
for /f "tokens=1-3 delims=:." %%h in ("%time%") do ( set _H=%%h & set _N=%%i & set _S=%%j )
set TS=!_Y!-!_M!-!_D!_!_H!-!_N!-!_S!
set PY=venv\Scripts\python.exe
if not exist "venv\Scripts\python.exe" ( py -3 -m venv venv || goto :crash )
"%PY%" -m pip install --upgrade pip >> "logs\setup_!TS!.log" 2>&1
"%PY%" -m pip install -r requirements.txt >> "logs\setup_!TS!.log" 2>&1
"%PY%" simian_launcher.py >> "logs\run_!TS!.log" 2>&1
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" goto :crash
echo OK
popd
exit /b 0
:crash
echo Crash. See logs\crash_!TS!.log
type "logs\setup_!TS!.log" >> "logs\crash_!TS!.log" 2>nul
type "logs\run_!TS!.log"   >> "logs\crash_!TS!.log" 2>nul
popd
pause
exit /b %EXITCODE%
