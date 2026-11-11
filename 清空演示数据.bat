@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   这会删掉当前的 data 文件夹（演示数据），
echo   程序下次启动就是一个全新的空账本。
echo.
echo   如果里面已经有你自己的真实记录，请不要继续！
echo.
pause
if not exist data (
    echo   没有 data 文件夹，本来就是空的。
    pause
    exit /b
)
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
if not exist "旧数据" mkdir "旧数据"
move /y data "旧数据\data_%STAMP%" >nul
echo.
echo   已移到 旧数据\data_%STAMP% ，要反悔就把它改回 data。
echo.
pause
