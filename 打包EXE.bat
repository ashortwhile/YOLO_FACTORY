@echo off
chcp 65001 >nul 2>&1
title Build YOLOStudio.exe launcher
cd /d "%~dp0"
where python >nul 2>&1
if not errorlevel 1 goto USEPYTHON
where py >nul 2>&1
if not errorlevel 1 goto USEPY
echo [ERROR] Python not found.
pause
exit /b 1

:USEPYTHON
python build_exe.py %*
goto DONE

:USEPY
py -3 build_exe.py %*
goto DONE

:DONE
pause
