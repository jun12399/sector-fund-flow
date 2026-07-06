@echo off
REM ============================================================
REM  A股资金流向监控 — 一键启动脚本
REM  用法: 双击运行，不要关闭窗口
REM ============================================================

title A股资金流向监控

echo.
echo ============================================================
echo   A股资金流向监控面板
echo ============================================================
echo.
echo 启动中...

cd /d %~dp0

REM 启动 Streamlit（IPv4+IPv6 双栈）
start "" "streamlit" run app_streamlit.py --server.port 8501 --server.headless true --server.address "::" 2>nul

REM 等 Streamlit 起来
timeout /t 4 /nobreak >nul

REM 获取公网 IPv6
for /f "tokens=*" %%i in ('python -c "import urllib.request; print(urllib.request.urlopen('https://api6.ipify.org', timeout=5).read().decode())" 2^>nul') do set IPV6=%%i

echo.
echo ============================================================
echo   ✅ 已启动！
echo.
echo   本地访问:    http://localhost:8501
echo   局域网访问:  http://192.168.3.6:8501
if not "%IPV6%"=="" (
    echo   公网 IPv6:   http://[%IPV6%]:8501
    echo.
    echo   💡 把 IPv6 地址发给朋友就能直接访问
)
echo.
echo   ⚠️  关闭此窗口即停止面板
echo   ⚠️  首次使用需以管理员身份运行防火墙命令:
echo       netsh advfirewall firewall add rule name="Streamlit 8501" dir=in action=allow protocol=tcp localport=8501
echo ============================================================

pause >nul
