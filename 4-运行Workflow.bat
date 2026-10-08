@echo off
title 多Agent工作流 - 编码 审查 修复
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set PYTHONUNBUFFERED=1

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ============================================================
    echo  还没有安装运行环境，暂时用不了。
    echo.
    echo  请先打开命令提示符，在文件夹里依次运行这两行：
    echo      python -m venv .venv
    echo      .venv\Scripts\pip install -r requirements.txt
    echo.
    echo  装好之后再双击本文件就可以了。
    echo ============================================================
    echo.
    pause
    exit /b
)

rem 中文必须在 chcp 之前存进变量：chcp 之后 cmd 会用 UTF-8 去读这个
rem GBK 编码的脚本，直接写在命令行里的中文会变成乱码。
set DEMO=写一个函数，把嵌套字典展平成单层字典，键之间用点号连接
set OUTDIR=runs\工作流演示

echo.
echo ============================================================
echo    多 Agent 工作流  ——  编码 → 审查 → 修复
echo ============================================================
echo.
echo  三个 AI 分工干活：
echo.
echo    [编码 Agent]  根据需求写出代码
echo    [审查 Agent]  挑毛病（复用作业一的审查流程）
echo    [修复 Agent]  按报告改代码，改完再送回审查
echo.
echo  这样循环，直到没有需要修的问题，或者到轮数上限。
echo  大约需要 20 到 60 秒。
echo.
echo ============================================================
echo.
echo  直接按回车用内置示例需求，或者自己输入一个需求：
echo.
echo    示例需求：  %DEMO%
echo.

set REQ=
set /p REQ=  需求（回车用示例）：

if "%REQ%"=="" set REQ=%DEMO%

echo.
echo  正在跑，请不要关窗口...
echo.

chcp 65001 >nul
".venv\Scripts\python.exe" -m codereview workflow "%REQ%" --out "%~dp0%OUTDIR%" --max-rounds 3 --quiet
chcp 936 >nul

echo.
echo ============================================================
echo  上面就是完整过程：每一步做了什么、报了几个问题、
echo  最终代码长什么样，都列出来了。
echo.
echo  完整报告（记事本可打开）：
echo    runs\工作流演示\workflow-report.md
echo.
echo  每一轮的代码快照也在同一个文件夹里，可以对比着看。
echo ============================================================
echo.
pause
