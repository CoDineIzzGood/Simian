\
@echo off

cd /d "D:\Project_C.H.I.M.P\Simian"
set "API=http://127.0.0.1:8000"

:: Read API key from .env
for /f %%A in ('powershell -NoP -C "(Get-Content .env | Where-Object {$_ -match '^SIMIAN_API_KEY='}) -replace '^SIMIAN_API_KEY=', ''"') do set "KEY=%%A"


curl -s -X POST %API%/api/gen/video ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %KEY%" ^
  -d "{ \"prompt\": \"neon waterfall, cinematic\", \"seconds\": 12, \"fps\": 24, \"preset\": \"hd\" }"
echo.
