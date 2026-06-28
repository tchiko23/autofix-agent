@echo off
REM ============================================================
REM  Lanceur de l'IHM web pour autofix-agent
REM  Demarre le backend FastAPI et ouvre le navigateur.
REM  A lancer depuis n'importe ou : il se place dans le bundle.
REM ============================================================

REM Se placer a la racine du bundle (dossier parent de webui\)
cd /d "%~dp0\.."

echo [WEBUI] Verification des dependances Python (fastapi, uvicorn)...
python -c "import fastapi, uvicorn" 2>NUL
if errorlevel 1 (
  echo [WEBUI] Installation de fastapi + uvicorn...
  python -m pip install fastapi "uvicorn[standard]"
  if errorlevel 1 (
    echo [WEBUI] ERREUR: echec de l'installation des dependances.
    pause
    exit /b 1
  )
)

echo [WEBUI] Demarrage du serveur sur http://127.0.0.1:8420 ...
echo [WEBUI] (Laisse cette fenetre ouverte. Ferme-la pour arreter le serveur.)

REM Ouvrir le navigateur apres un court delai, en parallele
start "" /b cmd /c "timeout /t 3 >NUL & start http://127.0.0.1:8420"

REM Lancer uvicorn (bloquant : la fenetre reste ouverte tant que le serveur tourne)
python -m uvicorn webui.server:app --host 127.0.0.1 --port 8420

pause
