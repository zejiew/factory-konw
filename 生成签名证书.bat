@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   生成代码签名证书（仅需做一次，或换电脑时做一次）
echo ============================================================
echo.
echo   这会创建一张自签名证书，并把它装进本机的信任存储。
echo   之后用 签名exe.bat 给程序签名，Windows 就认得你这个发行者。
echo.
echo   ⚠ 这只对本机有效。把 exe 发给别人时，对方仍会看到
echo     「未知发布者」——除非他也装了这张 .cer 证书。
echo.
pause

REM ---------- 检查是否已有证书 ----------
set "EXIST="
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert ^| Where-Object { $_.Subject -like '*CN=zejiew*' } ^| Select-Object -First 1).Thumbprint"') do set "EXIST=%%T"

if not "%EXIST%"=="" (
    echo.
    echo   已存在证书：%EXIST%
    echo.
    set /p REDO="   要重新生成一张吗？会替换掉旧的 (y/N): "
    if /i not "%REDO%"=="y" (
        echo   保留原有证书，退出。
        pause
        exit /b 0
    )
)

echo.
echo   [1/3] 创建证书...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$c = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=zejiew, O=zejiew, C=CN' -KeyUsage DigitalSignature -FriendlyName '代工厂物料台账 代码签名' -CertStoreLocation 'Cert:\CurrentUser\My' -NotAfter (Get-Date).AddYears(5) -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3','2.5.29.19={text}'); Write-Host ('   指纹: ' + $c.Thumbprint)"

if errorlevel 1 (
    echo   [失败] 创建证书出错。
    pause
    exit /b 1
)

echo.
echo   [2/3] 导出公钥证书...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$c = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -like '*CN=zejiew*' } | Select-Object -First 1; if ($c) { $out = Join-Path ([Environment]::GetFolderPath('Desktop')) '代工厂物料台账-代码签名证书.cer'; Export-Certificate -Cert $c -FilePath $out -Type CERT | Out-Null; Write-Host ('   已导出: ' + $out) } else { Write-Host '   导出失败：找不到证书'; exit 1 }"

echo.
echo   [3/3] 装入信任存储（受信任根 + 受信任发布者）...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$p = Join-Path ([Environment]::GetFolderPath('Desktop')) '代工厂物料台账-代码签名证书.cer'; $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($p); foreach ($n in @('Root','TrustedPublisher')) { $s = New-Object System.Security.Cryptography.X509Certificates.X509Store($n,'CurrentUser'); $s.Open('ReadWrite'); if (-not ($s.Certificates | Where-Object { $_.Thumbprint -eq $c.Thumbprint })) { $s.Add($c) }; $s.Close(); Write-Host ('   OK -> ' + $n) }"

echo.
echo ============================================================
echo   完成。接下来运行 签名exe.bat 给程序签名。
echo ============================================================
echo.
pause
