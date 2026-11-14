@echo off
rem 一次性管理员操作入口（UAC 确认）：授予当前账户「创建全局对象」权利。
rem 运行：右键「以管理员身份运行」本文件，或直接双击触发 UAC。
rem 授权在下次登录生效；回退方法见 grant-createglobal.ps1 头部注释。
net session >nul 2>&1
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0grant-createglobal.ps1"
) else (
    powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0grant-createglobal.ps1'"
)
echo.
type "%~dp0..\data\run\grant-global.log" 2>nul
echo 完成。请注销并重新登录一次，使特权生效。
pause
