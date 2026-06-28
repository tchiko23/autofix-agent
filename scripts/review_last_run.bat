@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "BUNDLE_DIR=%%~fI"
set "RUNS_DIR=%BUNDLE_DIR%\runs"

if not exist "%RUNS_DIR%" (
  echo [ERROR] runs directory not found: %RUNS_DIR%
  exit /b 1
)

for /f "delims=" %%i in ('dir /b /s /o-d "%RUNS_DIR%\summary.json" 2^>nul') do (
  set LAST_SUMMARY=%%i
  goto :found
)

echo [ERROR] no summary.json found under %RUNS_DIR%
exit /b 1

:found
echo [INFO] latest summary: %LAST_SUMMARY%
type "%LAST_SUMMARY%"
