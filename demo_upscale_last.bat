\
@echo off
setlocal EnableDelayedExpansion
cd /d "D:\Project_C.H.I.M.P\Simian"

:: API + key
set "API=http://127.0.0.1:8000"
for /f %%A in ('powershell -NoP -C "(Get-Content .env | Where-Object {$_ -match '^SIMIAN_API_KEY='}) -replace '^SIMIAN_API_KEY=', ''"') do set "KEY=%%A"

:: Find newest generated image
pushd "data\generated\images"
set "latest="
for /f "delims=" %%F in ('dir /b /a:-d /o:-d img_*.png 2^>nul') do (
  set "latest=%%F"
  goto :found
)
:found
popd

if not defined latest (
  echo No generated images found under data\generated\images\img_*.png
  exit /b 1
)

curl -s -X POST %API%/api/gen/upscale ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %KEY%" ^
  -d "{ \"input_path\": \"data/generated/images/%latest%\", \"target_w\": 1920, \"target_h\": 1080 }"
echo.
