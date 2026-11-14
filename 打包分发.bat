@echo off
chcp 65001 >nul 2>&1
title Build portable ZIP
cd /d "%~dp0"
where python >nul 2>&1
if not errorlevel 1 goto USEPYTHON
where py >nul 2>&1
if not errorlevel 1 goto USEPY
echo [ERROR] Python not found.
pause
exit /b 1

:USEPYTHON
python make_release.py %*
goto DONE

:USEPY
py -3 make_release.py %*
goto DONE

:DONE
pause
