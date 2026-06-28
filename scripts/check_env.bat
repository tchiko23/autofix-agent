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
  echo [ERROR] cannot enter BUNDLE_DIR: %BUNDLE_DIR%
  exit /b 1
)

REM v9.15 : charger FIX_TARGET_REPO depuis templates_external_config/config.env
call "%SCRIPT_DIR%_load_config.bat"

if defined FIX_TARGET_REPO (
  set "REPO_DIR=%FIX_TARGET_REPO%"
) else (
  set "REPO_DIR=%BUNDLE_DIR%\..\your-project\your-repo"
)
for %%I in ("%REPO_DIR%") do set "REPO_DIR=%%~fI"

REM Activation venv si dispo (warning sinon, non bloquant)
if exist "%BUNDLE_DIR%\.venv\Scripts\activate.bat" (
  call "%BUNDLE_DIR%\.venv\Scripts\activate.bat" >nul 2>nul
) else (
  echo [WARN] virtualenv absent, utilisation du Python systeme
)

REM Toutes les vérifications sont déléguées à check_env.py.
REM Sortie 0 = OK, 1 = au moins une vérification critique en échec.
python "%BUNDLE_DIR%\scripts\check_env.py" --bundle-dir "%BUNDLE_DIR%" --repo-dir "%REPO_DIR%"
set "ERR=%ERRORLEVEL%"
popd >nul
exit /b %ERR%
