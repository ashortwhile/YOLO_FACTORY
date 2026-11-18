@echo off
chcp 65001 >nul 2>&1
title 生成 YOLO 工坊 安装版 EXE
cd /d "%~dp0"
where python >nul 2>&1
if not errorlevel 1 goto USEPYTHON
where py >nul 2>&1
if not errorlevel 1 goto USEPY
echo [ERROR] Python not found. Install Python 3.9-3.12 first.
pause
exit /b 1

:USEPYTHON
echo 正在生成单文件安装程序，首次打包需要 1~2 分钟...
python build_setup.py %*
goto DONE

:USEPY
echo 正在生成单文件安装程序，首次打包需要 1~2 分钟...
py -3 build_setup.py %*
goto DONE

:DONE
echo.
pause
