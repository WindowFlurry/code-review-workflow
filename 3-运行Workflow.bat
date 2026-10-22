@echo off
setlocal
title 多 Agent 工作流 - 编码 审查 修复
cd /d "%~dp0"

rem 这个文件是 GBK 编码，全程不切换代码页。
rem 之前切到 UTF-8 之后，cmd 会把后面的中文当成 UTF-8 重新解析，
rem 结果就是乱码、甚至把 if 的括号吃掉，窗口直接闪退。
rem 现在反过来：让 Python 也按 GBK 输出，两边保持一致。
set PYTHONUTF8=0
set PYTHONIOENCODING=gbk:replace
set PYTHONPATH=%~dp0src
set PYTHONUNBUFFERED=1

set OUTDIR=runs\工作流

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ============================================================
    echo  还没有安装运行环境，暂时用不了。
    echo.
    echo  先打开命令行（在项目文件夹里按住 Shift 点右键，选「在终端中打开」），执行：
    echo      python -m venv .venv
    echo      .venv\Scripts\pip install -r requirements.txt
    echo.
    echo  装好之后回来双击这个文件就可以了。
    echo ============================================================
    echo.
    pause
    exit /b
)

echo.
echo ============================================================
echo    多 Agent 工作流    编码  ^>  审查  ^>  修复
echo ============================================================
echo.
echo  三个 Agent 各干一件事：
echo.
echo    [编码 Agent]  读需求       写代码
echo    [审查 Agent]  读代码       写问题清单      ^<-- 复用作一
echo    [修复 Agent]  读代码+清单  改写代码
echo         ^|
echo         +---- 修完送回再审，直到没有问题为止
echo.
echo ============================================================
echo.
echo  怎么用：
echo.
echo   1. 直接按回车       看示例（回放一次真实运行的结果，不花钱）
echo   2. 输入一句需求     AI 现场写代码并审查（会花几分钱）
echo   3. 把 .py 文件拖进来  现场审查你自己的代码（会花几分钱）
echo.

rem 注意：`set INPUT=` 在 cmd 里是「删除这个变量」，不是「设成空字符串」。
rem 删掉之后 `%INPUT%` 不再展开，后面就会冒出 if "%INPUT%"=="" 这种语法错误。
rem 所以这里先不管它，用 defined 判断，再处理引号。
set "INPUT="
set /p "INPUT=  请选择（直接回车看示例）："

set "HAS_INPUT="
if defined INPUT set "HAS_INPUT=1"
rem 拖拽进来的路径会被 Windows 自动包上一对引号，先去掉。
if defined INPUT set "INPUT=%INPUT:"=%"

echo.

if not defined HAS_INPUT (
    echo  [模式] 示例演示：回放一次真实运行的结果，不调用 AI，不花钱
    echo.
    ".venv\Scripts\python.exe" -m codereview demo
) else (
    if exist "%INPUT%" (
        echo  [模式] 现场审查你自己写的代码：%INPUT%
        echo.
        ".venv\Scripts\python.exe" -m codereview workflow --code "%INPUT%" --out "%OUTDIR%" --max-rounds 3 --quiet
    ) else (
        echo  [模式] 现场让 AI 写代码并审查，需求：%INPUT%
        echo.
        ".venv\Scripts\python.exe" -m codereview workflow "%INPUT%" --out "%OUTDIR%" --max-rounds 3 --quiet
    )
)

echo.
echo ============================================================
echo  看这张表的三列：
echo.
echo    谁 + 读 -^> 写    就是 Agent 之间怎么传信息
echo    代码改动          就是修复 Agent 动了哪里
echo    停止原因          就是流程判断的结果
echo.
echo  上面是屏幕上的输出，完整报告在这里（每次运行一个时间戳文件夹）：
echo    %OUTDIR%\
echo.
echo  默认只保留最近 10 次运行，更早的会自动清理。
echo ============================================================
echo.
pause
