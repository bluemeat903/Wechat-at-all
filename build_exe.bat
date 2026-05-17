@echo off
REM ==========================================================
REM   一键打包 微信@所有人 工具为 Windows 单文件 exe
REM   纯键盘模拟 + uiautomation 扫描，无微信库依赖
REM ==========================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

echo [1/3] 检查 Python...
python --version
if errorlevel 1 (
    echo.
    echo   X 未检测到 Python，请先安装 Python 3.9 或更高版本
    pause
    exit /b 1
)

echo.
echo [2/3] 安装依赖 pyautogui + pyperclip + uiautomation + pyinstaller （清华镜像）...
python -m pip install --upgrade pip -i %MIRROR%
python -m pip install pyautogui pyperclip uiautomation pyinstaller -i %MIRROR%
if errorlevel 1 (
    echo   X 镜像安装失败，尝试默认源...
    python -m pip install pyautogui pyperclip uiautomation pyinstaller
    if errorlevel 1 (
        echo   X 安装失败
        pause
        exit /b 1
    )
)

echo.
echo [3/3] 打包成单文件 exe ...
python -m PyInstaller --noconfirm --onefile --windowed --name "WeChatAtAll" --collect-all uiautomation wechat_at_all_gui.py
if errorlevel 1 (
    echo   X 打包失败
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo   OK 打包完成！exe 在: dist\WeChatAtAll.exe
echo ==========================================================
echo.
pause
