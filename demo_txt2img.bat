\
@echo off

cd /d "D:\Project_C.H.I.M.P\Simian"
set "API=http://127.0.0.1:8000"

:: Read API key from .env
for /f %%A in ('powershell -NoP -C "(Get-Content .env | Where-Object {$_ -match '^SIMIAN_API_KEY='}) -replace '^SIMIAN_API_KEY=', ''"') do set "KEY=%%A"


curl -s -X POST %API%/api/gen/txt2img ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %KEY%" ^
  -d "{ \"prompt\": \"neon rain alley, cinematic\", \"engine\": \"auto\", \"width\": 1024, \"height\": 576 }"
echo.
