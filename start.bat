@echo off
title Debate Chain Server
echo.
echo  ========================================
echo   Debate Chain Server
echo   Claude x Codex x Gemini
echo  ========================================
echo.
cd /d D:\Custom_AI-Agent_Project\debate-chain
echo  Starting server...
echo  http://localhost:5000
echo.
start http://localhost:5000
py server.py
pause
