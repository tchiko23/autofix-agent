@echo off
REM v9.16.1 : helper de chargement de la config depuis templates_external_config/config.env
REM Charge TOUTES les variables de config.env dans l'environnement du shell,
REM pas seulement FIX_TARGET_REPO. Necessaire pour que batch_run.py voie
REM AGENT_BATCH_TICKET_TIMEOUT et les autres reglages.
REM Les variables deja definies dans le shell ne sont PAS ecrasees (priorite shell).

set "CFG_FILE="
if exist "%BUNDLE_DIR%\..\templates_external_config\config.env" (
  set "CFG_FILE=%BUNDLE_DIR%\..\templates_external_config\config.env"
) else if exist "%BUNDLE_DIR%\templates_external_config\config.env" (
  set "CFG_FILE=%BUNDLE_DIR%\templates_external_config\config.env"
)

if not defined CFG_FILE goto :eof

for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%CFG_FILE%") do (
  if not "%%A"=="" (
    if not "%%B"=="" (
      REM Ne pas ecraser une variable deja definie dans le shell
      call :setIfUndefined "%%A" "%%B"
    )
  )
)
goto :eof

:setIfUndefined
if not defined %~1 set "%~1=%~2"
goto :eof
