@echo off
chcp 65001 > nul
title Debate Chain
color 0A

cd /d D:\Custom_AI-Agent_Project\debate-chain

echo.
echo  ================================================
echo   Debate Chain - Flask + MCP
echo  ================================================
echo.

REM --- .env load ---
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
)

REM --- venv ---
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

REM --- deps ---
py -c "import flask" 2>nul || py -m pip install flask --quiet
py -c "import sklearn" 2>nul || py -m pip install scikit-learn --quiet
py -c "import google.generativeai" 2>nul || py -m pip install google-generativeai --quiet

REM --- launch Flask ---
start "Flask" cmd /k "cd /d D:\Custom_AI-Agent_Project\debate-chain && py server_patch.py"

ping -n 4 127.0.0.1 > nul

REM --- launch MCP ---
start "MCP" cmd /k "cd /d D:\Custom_AI-Agent_Project\debate-chain && py mcp_server.py"

ping -n 4 127.0.0.1 > nul

REM --- open UI ---
start "" "http://localhost:5000"

echo  Flask : http://localhost:5000
echo  MCP   : running in separate window
echo.
pause
