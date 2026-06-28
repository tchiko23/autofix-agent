# Web UI for autofix-agent

A small web interface to run check_env and batch_run from a screen instead of the .bat scripts. Live log streaming.

## Architecture

The browser does not run git, Ollama, or Python directly. A small local server bridges the two sides:

```
  React (webui/index.html)               FastAPI (webui/server.py)
  ------------------------               -------------------------
  check_env / batch_run buttons   --->   receives the action (WebSocket)
  live log window                 <---   spawns the script as subprocess
  MR recap                               streams each log line
                                         reads config.env (FIX_TARGET_REPO)
```

Everything runs locally on 127.0.0.1. Nothing gets exposed on the network. No secret reaches the browser.

## Prerequisites

- Python (the same one used for the bundle)
- The bundle scripts work from the command line
- templates_external_config/config.env filled in. FIX_TARGET_REPO required.

The server reads FIX_TARGET_REPO from config.env, the same way _load_config.bat did. You do not enter the path twice.

## Starting the UI

Double click:

```
webui\start_webui.bat
```

The script installs fastapi and uvicorn on first launch when needed. The script starts the server. The script opens the browser at http://127.0.0.1:8420.

Manual equivalent from the bundle root:

```
python -m pip install fastapi "uvicorn[standard]"
python -m uvicorn webui.server:app --host 127.0.0.1 --port 8420
```

Keep the window open. Closing the window stops the server.

## Usage

1. Check the environment. Runs check_env.py. The top strip shows the resolved repo and config file. [OK] lines turn green. [ERROR] lines turn red.
2. Run the batch. Runs batch_run.py on every ticket in the analysis/ folder. Options: base-branch, --no-reset, --stop-on-python-error.
3. Logs stream in real time. A summary table (ticket, status, MR link) builds automatically from the logs and runs/history.jsonl.

## What this UI does not do, intentional minimal scope

- No config editing from the screen. Edit config.env by hand.
- No per ticket selection. The batch processes everything in analysis/.
- No persistent run history in the page. Logs live in page memory.
- No authentication. Local VM use only.

To add these features later, see CONTRIBUTING.md.

## Files

```
webui/
  server.py        FastAPI backend, reads config.env, spawns scripts, WebSocket
  index.html       Standalone React UI, CDN based, no build, no Node required
  start_webui.bat  Windows launcher
  README.md        this file
```

## Troubleshooting

FIX_TARGET_REPO not found: set in templates_external_config/config.env.

The page stays empty: check the browser console (F12). The React and Babel CDN requires internet access. On an airgapped VM, host React locally. Open an issue when your case looks like this.

Command not found in the logs: python sits outside the PATH of the window. Launch the .bat from a terminal where python responds.

Server fails to start (port in use): change port 8420 in both start_webui.bat AND when launching manually.
