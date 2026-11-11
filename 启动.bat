@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw main.py
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    python main.py
    exit /b
)

echo.
echo   没有找到 Python。
echo   请到 https://www.python.org/downloads/ 下载安装，
echo   安装时记得勾选 "Add python.exe to PATH"，装完再双击本文件。
echo.
pause
