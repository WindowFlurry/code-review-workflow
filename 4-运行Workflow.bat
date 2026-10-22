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
set OUTDIR=runs\工作流

echo.
echo ============================================================
echo    多 Agent 工作流  ——  编码 → 审查 → 修复
echo ============================================================
echo.
echo  三个 AI 分工干活，它们靠一张「共享的纸」传递信息：
echo.
echo    [编码 Agent]  读「需求」      写「代码」
echo    [审查 Agent]  读「代码」      写「问题清单」   ^<-- 复用作业一
echo    [修复 Agent]  读「代码+问题」  改写「代码」
echo         ^|
echo         +---- 改完再送回审查，直到没问题或到轮数上限
echo.
echo ============================================================
echo.
echo  两种玩法：
echo.
echo   1. 让 AI 写代码 —— 直接输入一句需求
echo.
echo   2. 审你自己写的代码 —— 把 .py 文件拖进来
echo      （工作流会跳过编码 Agent，直接从审查开始）
echo.
echo  直接按回车 = 用示例需求：%DEMO%
echo.

set INPUT=
set /p INPUT=  需求或代码文件（回车用示例）：

rem 拖拽进来的路径会被 Windows 自动加上一对引号，这里先去掉
set INPUT=%INPUT:"=%

if "%INPUT%"=="" set INPUT=%DEMO%

echo.

chcp 65001 >nul

rem 输入的是一个真实存在的文件，就走「审我的代码」这条路
if exist "%INPUT%" (
    echo  [模式] 审你自己写的代码：%INPUT%
    echo.
    ".venv\Scripts\python.exe" -m codereview workflow --code "%INPUT%" --out "%~dp0%OUTDIR%" --max-rounds 3 --quiet
) else (
    echo  [模式] 让 AI 写代码，需求：%INPUT%
    echo.
    ".venv\Scripts\python.exe" -m codereview workflow "%INPUT%" --out "%~dp0%OUTDIR%" --max-rounds 3 --quiet
)

chcp 936 >nul

echo.
echo ============================================================
echo  上面就是完整过程：
echo.
echo    · 「读 → 写」那一列 = 三个 Agent 之间怎么传信息的
echo    · 「代码改动」那一段 = 修复 Agent 到底改了哪几行
echo.
echo  报告和每一轮的代码快照都在这里（找最新那个带时间的子目录）：
echo    runs\工作流\
echo ============================================================
echo.
pause
