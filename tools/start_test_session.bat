@echo off
rem 4분 시험 세션 (config=testrun) — 확인 후 tools\data\collect	estrun 폴더를 지우면 된다
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_session.ps1" -Test %*
