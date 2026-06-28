@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "BUNDLE_DIR=%%~fI"
if not exist "%BUNDLE_DIR%" (
  echo [ERROR] BUNDLE_DIR not found
  exit /b 1
)
pushd "%BUNDLE_DIR%" >nul 2>nul

if exist "%BUNDLE_DIR%\.venv\Scripts\activate.bat" (
  call "%BUNDLE_DIR%\.venv\Scripts\activate.bat" >nul 2>nul
) else (
  echo [WARN] virtualenv absent
)

echo [REFRESH] === RAG INDEX REFRESH v9.8 ===
echo [REFRESH] Mise a jour incrementale, plus rapide qu'un build complet.
echo.

python -m scripts.rag_index_cli --mode refresh
set "ERR=%ERRORLEVEL%"
popd >nul
exit /b %ERR%
