"""Minimal manual synchronization and an in-memory local dashboard."""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser

from .config import ConfigError, DEFAULT_URL, ScraperConfig
from .service import DeadlineService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="龙谷大学 manaba 作业只读同步（日本时间）")
    commands = parser.add_subparsers(dest="command")
    once = commands.add_parser("sync", help="正常登录本人账号后，手动同步一次；只输出数量与通用状态")
    once.add_argument("--url", default=DEFAULT_URL, help="龙谷大学 manaba 网页 URL")
    once.add_argument("--login-timeout", type=int, default=300, help="等待本人登录的秒数（默认 300）")
    for command in ("serve", "watch"):
        server = commands.add_parser(command, help="运行本机小面板与自动同步（watch 为 serve 的别名）")
        server.add_argument("--url", default=DEFAULT_URL, help="龙谷大学 manaba 网页 URL")
        server.add_argument("--port", type=int, default=8765, help="127.0.0.1 本地端口（默认 8765）")
        server.add_argument("--interval", type=int, default=600, help="自动同步间隔秒数（默认 600，最小 60）")
        server.add_argument("--no-open", action="store_true", help="打印本地地址而不自动打开面板")
    return parser


async def _sync(config: ScraperConfig, login_timeout: int) -> int:
    async with DeadlineService(config) as service:
        print("正在打开独立浏览器。请在浏览器中正常登录本人校园账号。")
        if not await service.login(timeout_seconds=login_timeout):
            print(service.status()["message_zh"])
            return 1
        result = service.last_result
        if result is None:
            print(service.status()["message_zh"])
            return 1
        print(f"同步完成：课程 {len(result.courses)} 个，作业 {len(result.assignments)} 条，提示 {len(result.issues)} 条。")
        print("读取完整。" if result.complete else "读取不完整；请使用 serve 面板查看通用提示。")
        print("本次结果仅保留在内存中；一次同步命令退出后清空。")
        return 0 if result.complete else 2


async def _serve(config: ScraperConfig, port: int, open_panel: bool) -> int:
    from .webui import LocalDashboard

    async with DeadlineService(config) as service:
        dashboard = LocalDashboard(service, asyncio.get_running_loop(), port)
        try:
            dashboard.start()
            service.start_scheduler()
            print(f"本地面板：{dashboard.url}")
            print("打开面板后点击“打开登录窗口”，在浏览器正常登录。按 Ctrl+C 关闭并清空内存会话。")
            if open_panel:
                await asyncio.to_thread(webbrowser.open, dashboard.url)
            await asyncio.Event().wait()
        finally:
            await dashboard.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve"])
    try:
        config = ScraperConfig(url=args.url, interval_seconds=getattr(args, "interval", 600))
        if args.command == "sync":
            if args.login_timeout <= 0:
                parser.error("等待登录的秒数必须大于零。")
            exit_code = asyncio.run(_sync(config, args.login_timeout))
        else:
            if not 1 <= args.port <= 65535:
                parser.error("端口必须在 1–65535 之间。")
            exit_code = asyncio.run(_serve(config, args.port, not args.no_open))
    except ConfigError:
        print("配置无效。仅支持龙谷大学 manaba；同步间隔至少为 60 秒。", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("已关闭，作业与登录会话已清空。")
        exit_code = 0
    except OSError:
        print("本地端口不可用。请通过 --port 选择其他端口后重试。", file=sys.stderr)
        exit_code = 1
    except Exception:
        # Do not print arbitrary exceptions containing account/page data.
        print("启动失败。请确认依赖与 Chromium 已安装。", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
