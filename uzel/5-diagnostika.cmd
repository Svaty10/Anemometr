@echo off
rem Dvojklik = nahraje DIAGNOSTICKY firmware (test radia a desky). Navod: ..\docs\diagnostika.md
rem Zpet na normalni firmware: 3-aktualizace-firmwaru.cmd (parovani zustava)
chcp 65001 >nul
title Diagnosticky firmware
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -Diagnostika %*
echo.
pause
