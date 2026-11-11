@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   给 exe 签名（消除 SmartScreen 的「未知发布者」提示）
echo ============================================================
echo.
echo   作用：用本机自签名证书签名，Windows 会把发行者识别为
echo         zejiew，不再每次弹「Windows 已保护你的电脑」。
echo.
echo   注意：证书必须已装进本机的「受信任根」和「受信任发布者」。
echo         如果换电脑，请先运行一次 生成签名证书.bat
echo.

REM ---------- 找 signtool ----------
set "SIGNTOOL="
for %%V in (10.0.26100.0 10.0.22621.0 10.0.22000.0 10.0.19041.0 10.0.18362.0) do (
    if exist "C:\Program Files (x86)\Windows Kits\10\bin\%%V\x64\signtool.exe" (
        set "SIGNTOOL=C:\Program Files (x86)\Windows Kits\10\bin\%%V\x64\signtool.exe"
        goto :found_tool
    )
)
:found_tool
if "%SIGNTOOL%"=="" (
    echo   [错误] 没找到 signtool.exe
    echo   请安装 Windows SDK 后重试，或手动指定路径。
    echo.
    pause
    exit /b 1
)
echo   签名工具: %SIGNTOOL%
echo.

REM ---------- 找证书指纹 ----------
set "THUMB="
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert ^| Where-Object { $_.Subject -like '*CN=zejiew*' } ^| Select-Object -First 1).Thumbprint"') do set "THUMB=%%T"

if "%THUMB%"=="" (
    echo   [错误] 本机没有找到签名证书。
    echo   请先运行一次 生成签名证书.bat
    echo.
    pause
    exit /b 1
)
echo   证书指纹: %THUMB%
echo.

REM ---------- 找 exe ----------
set "TARGET="
if exist "dist\代工厂物料台账\代工厂物料台账.exe" (
    set "TARGET=dist\代工厂物料台账\代工厂物料台账.exe"
) else if exist "代工厂物料台账.exe" (
    set "TARGET=代工厂物料台账.exe"
)

if "%TARGET%"=="" (
    echo   [错误] 没找到要签名的 exe
    echo   请先打包：python -m PyInstaller ... main.py
    echo.
    pause
    exit /b 1
)
echo   目标文件: %TARGET%
echo.

REM 先关掉正在运行的实例，否则签名会占用冲突
taskkill /F /IM "代工厂物料台账.exe" >nul 2>nul

echo   正在签名...
"%SIGNTOOL%" sign /sha1 %THUMB% /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" "%TARGET%"
if errorlevel 1 (
    echo.
    echo   [失败] 签名出错，见上面的详细信息。
    pause
    exit /b 1
)

echo.
echo   正在验证...
powershell -NoProfile -Command "$s = Get-AuthenticodeSignature '%TARGET%'; Write-Host ('   状态: ' + $s.Status); Write-Host ('   签名者: ' + $s.SignerCertificate.Subject)"

echo.
echo ============================================================
echo   完成。现在双击 exe 不会再弹 SmartScreen 了。
echo ============================================================
echo.
pause
