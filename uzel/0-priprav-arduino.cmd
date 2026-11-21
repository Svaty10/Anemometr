@echo off
rem Dvojklik = z Arduina Uno/Nano udela programator jtag2updi (pin 6). Navod: ..\docs\programovani.md
chcp 65001 >nul
title Priprava Arduina (jtag2updi)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -PripravArduino %*
echo.
pause
