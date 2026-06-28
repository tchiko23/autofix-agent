@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "BUNDLE_DIR=%%~fI"
if not exist "%BUNDLE_DIR%" goto :error
pushd "%BUNDLE_DIR%" >nul 2>nul || goto :error

REM v9.15 : charger FIX_TARGET_REPO depuis templates_external_config/config.env
call "%SCRIPT_DIR%_load_config.bat"

if defined FIX_TARGET_REPO (
  set "REPO_DIR=%FIX_TARGET_REPO%"
) else (
  set "REPO_DIR=%BUNDLE_DIR%\..\your-project\your-repo"
)
for %%I in ("%REPO_DIR%") do set "REPO_DIR=%%~fI"

set "ANALYSIS_FILE=%BUNDLE_DIR%\analysis\issue_analysis.txt"
set "RUNS_DIR=%BUNDLE_DIR%\runs"

if not exist "%RUNS_DIR%" mkdir "%RUNS_DIR%"

if exist "%BUNDLE_DIR%\.venv\Scripts\activate.bat" (
  call "%BUNDLE_DIR%\.venv\Scripts\activate.bat" || goto :error
) else (
  echo [WARN] virtualenv absent, utilisation du Python systeme
)

echo [RUN] bundle=%BUNDLE_DIR%
echo [RUN] repo=%REPO_DIR%
echo [RUN] analysis=%ANALYSIS_FILE%
echo [RUN] runs=%RUNS_DIR%
python -m app.main --repo "%REPO_DIR%" --analysis-file "%ANALYSIS_FILE%" --dry-run
set "ERR=%ERRORLEVEL%"
popd >nul
exit /b %ERR%

:error
echo [ERROR] dry run setup failed
popd >nul 2>nul
exit /b 1
