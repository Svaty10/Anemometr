@echo off
rem Dvojklik = vycte z cidla vysledek diagnostiky a vysvetli ho. Navod: ..\docs\diagnostika.md
chcp 65001 >nul
title Vysledek diagnostiky
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -Cti %*
echo.
pause
