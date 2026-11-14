@echo off
rem Java NIO selector 一键自检（排查 Neo4j「监听不应答」用）
rem 判定：SELECTOR-OK  = 事件投递正常，Neo4j 应可正常工作
rem       SELECTOR-DEAF = WEPoll 事件投递仍失聪（机器级，重启/排查驱动）
rem 建议：重启后、未打开 FlClash 时测，能一次分辨「瞬时状态」vs「FlClash/wintun」
cd /d "%~dp0.."
set LOG=%TEMP%\nio_test.log
start "" /b services\jdk17full\bin\java.exe deploy\NioSelectorTest.java 17888 > "%LOG%" 2>&1
timeout /t 3 /nobreak >nul
powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient('127.0.0.1',17888)).Close(); Write-Host 'client connected' } catch { Write-Host ('client fail: ' + $_.Exception.Message) }"
timeout /t 9 /nobreak >nul
type "%LOG%"
echo.
echo ==============================
echo SELECTOR-OK   = 事件投递正常（下一步: bash deploy/start.sh 完成四服务验收）
echo SELECTOR-DEAF = 仍失聪（保持 FlClash 关闭重启电脑后再测）
pause
