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

if defined FIX_BASE_BRANCH (
  set "BASE_BRANCH=%FIX_BASE_BRANCH%"
) else (
  set "BASE_BRANCH=main"
)

set "ANALYSIS_DIR=%BUNDLE_DIR%\analysis"
set "RUNS_DIR=%BUNDLE_DIR%\runs"

if not exist "%RUNS_DIR%" mkdir "%RUNS_DIR%"

if exist "%BUNDLE_DIR%\.venv\Scripts\activate.bat" (
  call "%BUNDLE_DIR%\.venv\Scripts\activate.bat" || goto :error
) else (
  echo [WARN] virtualenv absent, utilisation du Python systeme
)

echo [BATCH] bundle=%BUNDLE_DIR%
echo [BATCH] repo=%REPO_DIR%
echo [BATCH] analysis_dir=%ANALYSIS_DIR%
echo [BATCH] base_branch=%BASE_BRANCH%
echo [BATCH] runs=%RUNS_DIR%
echo.
echo [BATCH] Conventions de nommage attendues dans %ANALYSIS_DIR% :
echo   - issue_analysis_TICKET-XXXXX.txt
echo   - RUN-XXXXX.txt
echo.
python "%BUNDLE_DIR%\scripts\batch_run.py" --repo "%REPO_DIR%" --analysis-dir "%ANALYSIS_DIR%" --base-branch %BASE_BRANCH% %*
set "ERR=%ERRORLEVEL%"
popd >nul
exit /b %ERR%

:error
echo [ERROR] batch run setup failed
popd >nul 2>nul
exit /b 1
