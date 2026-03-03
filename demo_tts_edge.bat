\
@echo off

cd /d "D:\Project_C.H.I.M.P\Simian"
set "API=http://127.0.0.1:8000"

:: Read API key from .env
for /f %%A in ('powershell -NoP -C "(Get-Content .env | Where-Object {$_ -match '^SIMIAN_API_KEY='}) -replace '^SIMIAN_API_KEY=', ''"') do set "KEY=%%A"


:: Use a valid Edge voice name (no 'edge_' prefix)
set "VOICE=en-US-GuyNeural"

curl -s -X POST %API%/api/gen/tts ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %KEY%" ^
  -d "{ \"text\": \"Simian online and generating.\", \"voice\": \"%VOICE%\" }"
echo.
