@echo off
chcp 65001 >nul 2>&1
title YOLO Studio - Local Service
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto USEPY
where python >nul 2>&1
if not errorlevel 1 goto USEPYTHON
echo [ERROR] Python 3.9 - 3.13 not found. Install from https://www.python.org/downloads/
pause
exit /b 1

:USEPY
py -3 bootstrap.py %*
goto DONE

:USEPYTHON
python bootstrap.py %*
goto DONE

:DONE
if errorlevel 1 (
  echo [ERROR] see messages above.
  pause
)
