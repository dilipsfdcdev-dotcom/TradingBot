@echo off
rem Starts the TradingView -> MT5 bridge. Restarts it automatically if it ever exits.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" -m bridge
echo Bridge stopped. Restarting in 3 seconds... (Ctrl+C to quit)
timeout /t 3 /nobreak >nul
goto loop
