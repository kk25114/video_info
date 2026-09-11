@echo off
setlocal

rem Windows 双击/命令提示符入口；参数会原样传给 publish_latest.ps1。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_latest.ps1" %*
exit /b %ERRORLEVEL%
