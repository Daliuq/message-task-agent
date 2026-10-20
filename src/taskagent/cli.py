from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AgentWorker, JsonCommandAgent, NoopAgent
from .mail import load_saved_credentials, scan_mailbox, scan_mailbox_range
from .notifier import send_next_notification
from .store import Store
from .webui import serve_ui

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "taskagent.sqlite3"
DEFAULT_DATA = ROOT / "data"


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db, args.data_dir)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _scan_mail(args: argparse.Namespace) -> int:
    credentials = load_saved_credentials()
    if not credentials:
        print("未找到现有 QQ 邮箱凭据；请先使用 QQ 邮箱客户端保存配置。", file=sys.stderr)
        return 2
    result = scan_mailbox(_store(args), credentials["address"], credentials["auth_code"],
                          max_per_folder=args.max_per_folder, import_history=args.import_history,
                          folders=args.folder, newest_first=args.newest_first)
    _print(result)
    return 0 if result["ok"] else 1


def _scan_mail_range(args: argparse.Namespace) -> int:
    credentials = load_saved_credentials()
    if not credentials:
        print("未找到现有 QQ 邮箱凭据；请先使用 QQ 邮箱客户端保存配置。", file=sys.stderr)
        return 2
    result = scan_mailbox_range(_store(args), credentials["address"], credentials["auth_code"],
                                args.start, args.end, folders=args.folder,
                                batch_size=args.batch_size)
    _print(result)
    return 0 if result["ok"] else 1


def _worker(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.noop:
        agent = NoopAgent()
    elif args.command_json:
        command = json.loads(args.command_json)
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("--command-json 必须是字符串数组，例如 [\"python\",\"agent.py\"]")
        agent = JsonCommandAgent(command, timeout_seconds=args.timeout)
    else:
        print("请配置 JSON Agent 命令，或显式传入 --noop 进行开发测试。", file=sys.stderr)
        return 2
    worker = AgentWorker(store, agent)
    handled = 0
    while True:
        result = worker.process_once()
        if result is None:
            break
        _print(result)
        handled += 1
        if args.once or result.get("status") == "failed":
            break
    print(f"已消费事件数：{handled}")
    return 0


def _status(args: argparse.Namespace) -> int:
    store = _store(args)
    _print({"counts": store.stats(), "tasks": store.tasks(),
            "email_outbox_preview": store.outbox_preview()})
    return 0


def _preview(args: argparse.Namespace) -> int:
    _print(_store(args).outbox_preview(args.limit))
    return 0


def _send_email(args: argparse.Namespace) -> int:
    if not args.confirm_send:
        print("实际 SMTP 投递需要显式传入 --confirm-send。", file=sys.stderr)
        return 2
    credentials = load_saved_credentials()
    if not credentials:
        print("未找到现有 QQ 邮箱凭据。", file=sys.stderr)
        return 2
    result = send_next_notification(_store(args), credentials["address"],
                                    credentials["auth_code"], args.to)
    _print(result or {"status": "empty", "message": "没有待发送邮件"})
    return 1 if result and result["status"] in {"failed", "delivery_unknown"} else 0


def _set_status(args: argparse.Namespace) -> int:
    _store(args).set_task_status(args.task_id, args.status)
    print(f"任务 {args.task_id} 状态已更新为 {args.status}")
    return 0


def _plan(args: argparse.Namespace) -> int:
    from .planner import plan_day

    result = plan_day(_store(args), args.date, args.window, busy=args.busy,
                      time_limit_seconds=args.time_limit)
    _print(result)
    return 0 if result["solver_status"] in {"OPTIMAL", "FEASIBLE"} else 1


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="message-task-agent", description="本地消息增量与任务管理原型")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="邮件原文和本地数据目录")
    commands = parser.add_subparsers(dest="subcommand", required=True)

    commands.add_parser("init", help="初始化本地数据库")
    commands.add_parser("scan-scheduled", help="扫描全部本机定时任务结果与 ChatGPT 结果收件箱，不调用模型或修改定时安排")
    scheduled_import = commands.add_parser("import-scheduled", help="保存并导入标准定时任务结果包")
    scheduled_import.add_argument("file", type=Path)
    chats = commands.add_parser("scan-chats", help="导入已订阅聊天正文（不调用 LLM、不发送消息）")
    chats.add_argument("--platform", choices=("wechat", "qq"), default="wechat")
    chats.add_argument("--mode", choices=("full", "range", "incremental"), default="full")
    chats.add_argument("--scope", action="append")
    chats.add_argument("--start")
    chats.add_argument("--end")
    chats.add_argument("--batch-size", type=int, default=200)
    chat_status = commands.add_parser("chat-import-status", help="查看聊天导入进度")
    chat_status.add_argument("--job")
    chat_resume = commands.add_parser("chat-import-resume", help="继续未完成的聊天导入")
    chat_resume.add_argument("job_id")
    mail = commands.add_parser("scan-mail", help="只读扫描已保存配置的 QQ 邮箱全部可列出文件夹")
    mail.add_argument("--max-per-folder", type=int, default=25)
    mail.add_argument("--import-history", action="store_true", help="仅对新文件夹导入历史；默认建立当前 UID 基线")
    mail.add_argument("--folder", action="append", help="只扫描指定文件夹；可重复传入")
    mail.add_argument("--newest-first", action="store_true",
                      help="仅配合新文件夹的 --import-history 和限量抽样；从高 UID 开始，不用于完整历史回补")

    range_scan = commands.add_parser("scan-mail-range", help="按收件时间全量回补指定范围邮件，不修改增量游标")
    range_scan.add_argument("--start", required=True,
                            help="范围开始点；日期 YYYY-MM-DD 按北京时间当天 00:00，含开始点")
    range_scan.add_argument("--end", required=True,
                            help="范围结束；日期 YYYY-MM-DD 含当天，时间戳 ISO 格式则作为不含结束点")
    range_scan.add_argument("--folder", action="append", help="只扫描指定文件夹；可重复传入，默认全部可列出文件夹")
    range_scan.add_argument("--batch-size", type=int, default=50,
                            help="单批持久化的最大候选邮件数，1-500，默认 50")

    worker = commands.add_parser("worker", help="独立消费已持久化的事件")
    worker.add_argument("--command-json", help="自定义 Agent 命令数组 JSON；shell=False，事件内容通过 stdin")
    worker.add_argument("--timeout", type=int, default=120)
    worker.add_argument("--noop", action="store_true", help="明确使用空 Agent，仅验证消费链路")
    worker.add_argument("--once", action="store_true", help="最多处理一个事件")

    commands.add_parser("status", help="查看本地计数、任务与邮件预览")
    export = commands.add_parser("export-batch", help="导出消息、提示词和网页解析请求（仅本地）")
    export.add_argument("--key", required=True, help="稳定请求键；重试必须沿用")
    export.add_argument("--mode", choices=("pending", "incremental", "range"), default="pending")
    export.add_argument("--scope", action="append", help="本地 scope ID；省略则包含所有已落库来源")
    export.add_argument("--start", help="范围起点，带时区 ISO 时间，含")
    export.add_argument("--end", help="范围终点，带时区 ISO 时间，不含")
    export.add_argument("--limit", type=int, default=100)
    inspect_import = commands.add_parser("import-preview", help="预检消息包／解析结果，不应用")
    inspect_import.add_argument("kind", choices=("messages", "results"))
    inspect_import.add_argument("file", type=Path)
    apply_import = commands.add_parser("import-apply", help="确认应用此前的导入预检（不发邮件）")
    apply_import.add_argument("preview_id")
    commands.add_parser("exchange-status", help="查看导出批次和待确认解析候选")
    resume = commands.add_parser("export-resume", help="恢复批次文件输出")
    resume.add_argument("batch_id")
    cancel = commands.add_parser("export-cancel", help="取消请求并释放尚未成功解析的事件")
    cancel.add_argument("batch_id")
    ui = commands.add_parser("ui", help="启动本机可视化任务面板")
    ui.add_argument("--host", default="127.0.0.1", help="仅允许本机回环地址，默认 127.0.0.1")
    ui.add_argument("--port", type=int, default=8765, help="本地服务端口，默认 8765")
    preview = commands.add_parser("email-preview", help="查看待发送邮件预览，不会发送")
    preview.add_argument("--limit", type=int, default=50)
    send_email = commands.add_parser("send-email", help="发送一封 outbox 邮件（需要显式确认）")
    send_email.add_argument("--to", required=True, help="收件邮箱地址")
    send_email.add_argument("--confirm-send", action="store_true", help="确认执行真实 SMTP 投递")

    mark = commands.add_parser("task-status", help="手工更新任务状态")
    mark.add_argument("task_id")
    mark.add_argument("status", choices=("open", "completed", "deferred", "cancelled"))

    planner = commands.add_parser("plan", help="按明确的可用时间生成单日任务排程")
    planner.add_argument("--date", required=True, help="规划日期 YYYY-MM-DD")
    planner.add_argument("--window", action="append", required=True,
                         help="可安排时间窗 HH:MM-HH:MM，可重复传入")
    planner.add_argument("--busy", action="append", default=[],
                         help="不可用时段 HH:MM-HH:MM=名称，可重复传入")
    planner.add_argument("--time-limit", type=float, default=5.0, help="CP-SAT 求解时限秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "init":
            store = _store(args)
            _print({"initialized": True, "database": str(store.db_path), "data_dir": str(store.data_dir)})
            return 0
        if args.subcommand == "scan-mail":
            return _scan_mail(args)
        if args.subcommand in {"scan-scheduled", "import-scheduled"}:
            from .scheduled_results import ScheduledResults
            reader = ScheduledResults(_store(args))
            if args.subcommand == "import-scheduled":
                _print(reader.import_packet(json.loads(args.file.read_text(encoding="utf-8-sig"))))
                return 0
            job = reader.start()
            result = reader.run(job["id"])
            _print(result)
            return 0 if all(s["status"] != "failed" for s in result["sources"]) else 1
        if args.subcommand in {"scan-chats", "chat-import-status", "chat-import-resume"}:
            from .chat_import import ChatImports
            imports = ChatImports(_store(args))
            if args.subcommand == "chat-import-status":
                _print(imports.job(args.job) if args.job else imports.jobs())
                return 0
            job = imports.create(platform=args.platform, mode=args.mode, scope_ids=args.scope,
                start=args.start, end=args.end, batch_size=args.batch_size) if args.subcommand == "scan-chats" else {"id": args.job_id}
            _print({"started_job_id": job["id"]})
            result = imports.run(job["id"])
            _print(result)
            return 0 if result["status"] == "completed" else 1
        if args.subcommand == "scan-mail-range":
            return _scan_mail_range(args)
        if args.subcommand == "worker":
            return _worker(args)
        if args.subcommand == "status":
            return _status(args)
        if args.subcommand in {"export-batch", "import-preview", "import-apply", "exchange-status",
                               "export-resume", "export-cancel"}:
            from .exchange import Exchange
            from .contracts import loads_document
            exchange = Exchange(_store(args))
            if args.subcommand == "export-batch":
                _print(exchange.create_export(idempotency_key=args.key, mode=args.mode, scope_ids=args.scope,
                                              start=args.start, end=args.end, limit=args.limit))
            elif args.subcommand == "import-preview":
                _print(exchange.preview(args.kind, loads_document(args.file.read_text(encoding="utf-8-sig"))))
            elif args.subcommand == "import-apply":
                _print(exchange.apply(args.preview_id))
            elif args.subcommand == "exchange-status":
                _print({"batches": exchange.batches(), "candidates": exchange.candidates()})
            elif args.subcommand == "export-resume":
                _print(exchange.finish_export(args.batch_id))
            else:
                _print(exchange.cancel(args.batch_id))
            return 0
        if args.subcommand == "ui":
            serve_ui(args.db, args.data_dir, args.host, args.port)
            return 0
        if args.subcommand == "email-preview":
            return _preview(args)
        if args.subcommand == "send-email":
            return _send_email(args)
        if args.subcommand == "task-status":
            return _set_status(args)
        if args.subcommand == "plan":
            return _plan(args)
        parser.error("未知命令")
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
