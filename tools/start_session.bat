@echo off
rem 더블클릭용 — 실제 로직은 start_session.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_session.ps1" %*
