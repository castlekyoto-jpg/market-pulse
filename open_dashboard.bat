@echo off
cd /d "%~dp0"
start "" "http://localhost:8000"
py -3 -m http.server 8000
