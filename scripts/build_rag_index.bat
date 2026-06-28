@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "BUNDLE_DIR=%%~fI"
if not exist "%BUNDLE_DIR%" (
  echo [ERROR] BUNDLE_DIR not found: %BUNDLE_DIR%
  exit /b 1
)
pushd "%BUNDLE_DIR%" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] cannot enter BUNDLE_DIR
  exit /b 1
)

REM Activation venv si dispo
if exist "%BUNDLE_DIR%\.venv\Scripts\activate.bat" (
  call "%BUNDLE_DIR%\.venv\Scripts\activate.bat" >nul 2>nul
) else (
  echo [WARN] virtualenv absent, utilisation du Python systeme
)

echo [BUILD] === RAG INDEX BUILDER v9.8 ===
echo [BUILD] Cette etape peut prendre 1-3h selon le volume.
echo [BUILD] Premier lancement = telechargement du modele d'embedding ~120 Mo.
echo.

python -m scripts.rag_index_cli --mode build
set "ERR=%ERRORLEVEL%"
popd >nul
exit /b %ERR%
