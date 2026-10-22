"""给当前版本做还原点：完整备份到 D 盘，并生成一键还原脚本。

用法::

    python scripts/snapshot.py v1.0

会在 ``D:\\agent备份\\<版本名>\\`` 生成整个项目的完整副本（**含 .venv**），
并在 ``D:\\agent备份\\`` 下生成 ``还原 <版本名>.bat`` 和 ``说明.txt``。

为什么用 Python 调 robocopy，而不是直接在 .bat 里写 robocopy：
中文路径从 cmd 传给 robocopy 时会因为代码页问题变成乱码，报「无效参数」。
Python 在 Windows 上通过宽字符 API 传参数，不存在这个问题。
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

#: 备份根目录。放在 D 盘，不占 C 盘，也不弄乱桌面。
BACKUP_ROOT = Path(r"D:\agent备份")

#: 不备份的目录（缓存，随时能重新生成）。
EXCLUDE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache")

#: robocopy 退出码 0-7 都表示成功，8 及以上才是真出错。
_ROBOCOPY_OK_MAX = 7


def _run_robocopy(source: Path, destination: Path, *, mirror: bool) -> int:
    """调用 robocopy。

    ``mirror=False`` 用于首次备份（/E：复制所有子目录）。
    ``mirror=True`` 用于还原（/MIR：让目标与备份完全一致，多余的删掉）。
    """
    argv = [
        "robocopy",
        str(source),
        str(destination),
        "/MIR" if mirror else "/E",
        "/NFL",  # 不逐文件列名字
        "/NDL",  # 不列目录名
        "/NP",  # 不显示进度百分比
        "/R:2",  # 单个文件失败重试 2 次
        "/W:1",  # 重试间隔 1 秒
        "/XD", *EXCLUDE_DIRS,
    ]

    completed = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    return completed.returncode


def _describe(path: Path) -> tuple[int, float]:
    """返回 ``(文件数, 总MB)``。"""
    count = 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            count += 1
            # 少数文件可能正被占用而读不到大小，跳过就行，不该因此中断统计。
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return count, total / (1024 * 1024)


def _write_restore_bat(version: str, destination: Path) -> Path:
    """生成一键还原脚本。

    刻意放在**备份目录旁边**而不是项目文件夹里：还原操作要覆盖整个项目文件夹，
    脚本如果自己也在被覆盖的范围内，Windows 会因为「文件被占用」而失败。

    编码必须是 GBK + CRLF：cmd.exe 按控制台代码页（中文系统是 936）读取 .bat，
    UTF-8 的中文会让它解析错位，后面的行会整个乱掉。
    """
    project = Path(__file__).resolve().parents[1]

    content = f"""@echo off
title 还原到 {version}
chcp 936 >nul

echo.
echo ============================================================
echo    还原到 {version}
echo ============================================================
echo.
echo  这个操作会用备份的内容覆盖项目文件夹：
echo.
echo    备份：  {destination}
echo    项目：  {project}
echo.
echo  当前项目里的代码、文档、运行环境都会被换成备份里的版本。
echo  只有 runs 文件夹（历史报告）会保留，不会被删。
echo.
echo  【如果项目里有你不想丢的改动，现在先按 Ctrl-C 退出】
echo.
echo ============================================================
echo.

set CONFIRM=
set /p CONFIRM=  确认还原请输入 yes：

if /i not "%CONFIRM%"=="yes" (
    echo.
    echo  已取消，什么都没有改动。
    echo.
    pause
    exit /b
)

echo.
echo  正在还原，请不要关窗口...

robocopy "{destination}" "{project}" /MIR /XD runs {chr(32).join(EXCLUDE_DIRS)} /NFL /NDL /NP /R:2 /W:1

if errorlevel 8 (
    echo.
    echo  还原过程中出现错误，请把上面的信息发给我看看。
    echo.
    pause
    exit /b
)

echo.
echo ============================================================
echo  还原完成！项目已经回到 {version} 的状态。
echo.
echo  现在可以双击项目里的 1-运行示例.bat 验证一下。
echo ============================================================
echo.
pause
"""

    path = BACKUP_ROOT / f"还原 {version}.bat"
    path.write_bytes(content.replace("\n", "\r\n").encode("gbk"))
    return path


def _write_readme(version: str, destination: Path, file_count: int, size_mb: float) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    content = f"""这是「代码审查助手」的备份（版本 {version}）
备份时间：{stamp}

────────────────────────────────────────────────────────
里面是什么
────────────────────────────────────────────────────────

  {destination}
     整个项目的完整副本，共 {file_count} 个文件，约 {size_mb:.0f} MB。
     包含了运行环境（.venv），所以拷回来不用重新安装就能直接用。

  还原 {version}.bat
     一键还原。双击它，输入 yes，项目就会回到 {version} 的状态。

  （这个说明文件）


────────────────────────────────────────────────────────
怎么用这个备份
────────────────────────────────────────────────────────

方法一：当还原点用
  项目改坏了、或者环境装崩了，双击「还原 {version}.bat」，
  输入 yes，就能退回现在这个能用的状态。

方法二：当独立副本用
  不想动原来的项目，也可以直接进 {destination}
  双击里面的 1-运行示例.bat，就当另一个副本用。
  （.venv 是绿色的，搬到哪里都能跑）


────────────────────────────────────────────────────────
注意事项
────────────────────────────────────────────────────────

⚠️  这个文件夹里有 .env 文件，里面是你的 AI 密钥。

    自己用没问题，但**不要把这个文件夹整个发给别人，
    也不要传到网盘或者群里**。要分享或提交代码的话，
    手动把 .env 和 .venv 排除掉再打包。

⚠️  还原是破坏性操作，会覆盖项目文件夹的当前内容。
    脚本要求输入 yes 才会执行，不会误触发。


────────────────────────────────────────────────────────
它防得住什么、防不住什么
────────────────────────────────────────────────────────

防得住：
  · 代码改坏了，想退回现在的版本
  · 误删了 .venv，或者 pip install 装崩了（不用重装）
  · 误删了项目文件夹
  · 重装 Windows（只影响 C 盘）

防不住：
  · 整块硬盘物理损坏 —— C 盘和 D 盘在同一块硬盘上，
    两个分区会一起没。

    这一部分由 GitHub 兜底：代码已经推到
    https://github.com/WindowFlurry/code-review-agent
    硬盘真坏了也能 git clone 回来，只是要重新 pip install
    装一次环境（约 5 分钟）。


────────────────────────────────────────────────────────
版本记录
────────────────────────────────────────────────────────

  v1.0   {stamp}
        作业提交版。分层审查流水线 + 证据核对，92 项测试通过。
        已推送到 GitHub，并打了 git 标签 v1.0。
"""

    path = BACKUP_ROOT / "说明.txt"
    path.write_text(content, encoding="utf-8-sig")  # BOM 让记事本正确识别 UTF-8
    return path


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法：python scripts/snapshot.py 版本名     例如 v1.0")
        return 2

    version = sys.argv[1].strip()
    for bad in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
        if bad in version:
            print(f"版本名里不能有 {bad!r}，请换一个。")
            return 2

    project = Path(__file__).resolve().parents[1]
    destination = BACKUP_ROOT / version

    if shutil.which("robocopy") is None:
        print("找不到 robocopy（Windows 自带，通常在 C:\\Windows\\System32）。")
        return 1

    if destination.exists():
        print(f"目标已存在：{destination}")
        print("换一个版本名，或者先把旧的手动删掉。")
        return 1

    print(f"正在备份：{project}")
    print(f"      到：{destination}")
    print("（约 450MB，需要一两分钟，请稍候……）")
    print()

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    code = _run_robocopy(project, destination, mirror=False)
    if code > _ROBOCOPY_OK_MAX:
        print(f"备份失败，robocopy 返回 {code}。")
        return 1

    file_count, size_mb = _describe(destination)
    if file_count == 0:
        print("备份目录是空的，说明复制没有成功。")
        return 1

    restore = _write_restore_bat(version, destination)
    readme = _write_readme(version, destination, file_count, size_mb)

    print("=" * 60)
    print(f"  备份完成：{destination}")
    print(f"  共 {file_count} 个文件，约 {size_mb:.0f} MB")
    print()
    print(f"  还原脚本：{restore}")
    print(f"  说明文件：{readme}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
