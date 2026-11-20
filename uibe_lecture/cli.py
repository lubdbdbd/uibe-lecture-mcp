from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def output(data):
    print(json.dumps(data,ensure_ascii=False,default=str))


def main():
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p=argparse.ArgumentParser(description="成长惠园自动讲座工具")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("mcp",help="启动 Codex MCP stdio 服务")
    sub.add_parser("worker",help="独立后台任务执行器").add_argument("--data-dir",type=Path)
    sub.add_parser("start",help="启动后台并立即返回")
    sub.add_parser("setup", help="Local interactive login and opt-in background setup")
    startup=sub.add_parser("startup").add_subparsers(dest="startup_action",required=True)
    startup.add_parser("enable"); startup.add_parser("disable")
    watchdog=sub.add_parser("watchdog", help="Recover the worker without submitting new jobs")
    watchdog.add_argument("--data-dir",type=Path,required=True)
    auth=sub.add_parser("auth").add_subparsers(dest="action",required=True)
    auth.add_parser("login"); auth.add_parser("status")
    auth.add_parser("configure"); auth.add_parser("forget")
    auth.add_parser("credentials-set").add_argument("--stdin",action="store_true",required=True)
    listing=sub.add_parser("list")
    listing.add_argument("--criteria",type=Path)
    listing.add_argument("--cached",action="store_true")
    sub.add_parser("reservations")
    show=sub.add_parser("show"); show.add_argument("id")
    reserve=sub.add_parser("reserve")
    reserve.add_argument("id"); reserve.add_argument("--instruction",required=True)
    reserve.add_argument("--watch-minutes",type=int,default=0)
    reserve.add_argument("--dry-run",action="store_true")
    rule=sub.add_parser("monitor"); rule.add_argument("rule_file",type=Path)
    status=sub.add_parser("status"); status.add_argument("id",nargs="?")
    stop=sub.add_parser("stop"); stop.add_argument("id")
    verify=sub.add_parser("verify"); verify.add_argument("id")
    a=p.parse_args()
    try:
        if a.command=="mcp":
            from .mcp_server import mcp
            from .service import start_worker
            from .storage import Store
            start_worker(Store())
            mcp.run(transport="stdio")
            return
        if a.command=="worker":
            if a.data_dir:
                import os
                os.environ["UIBE_DATA_DIR"]=str(a.data_dir.resolve())
            from .engine import Engine
            asyncio.run(Engine().run())
            return
        if a.command=="setup":
            from .onboarding import setup
            output({"ok":True,"data":setup()})
            return
        if a.command=="startup":
            from .startup import enable_startup,disable_startup
            result=enable_startup() if a.startup_action=="enable" else disable_startup()
            output({"ok":True,"data":result})
            return
        if a.command=="watchdog":
            import os
            os.environ["UIBE_DATA_DIR"]=str(a.data_dir.resolve())
        from .service import Service,start_worker
        s=Service()
        if a.command=="auth":
            if a.action=="login":
                from .auth import login
                result=asyncio.run(login())
            elif a.action=="credentials-set":
                from .credentials import configure_stdin
                result=configure_stdin()
            elif a.action=="configure":
                from .credentials import configure_interactive
                result=configure_interactive()
            elif a.action=="forget":
                from .credentials import delete_credentials
                delete_credentials()
                result={"deleted":True}
            else:
                result=asyncio.run(s.auth_status())
        elif a.command=="list":
            f=json.loads(a.criteria.read_text(encoding="utf-8-sig")) if a.criteria else {}
            result=asyncio.run(s.catalog(f,not a.cached))
        elif a.command=="show":
            result=asyncio.run(s.details(a.id))
        elif a.command=="reservations":
            result=asyncio.run(s.reservations())
        elif a.command=="reserve":
            result=asyncio.run(s.reserve(a.id,a.instruction,a.watch_minutes,dry_run=a.dry_run))
        elif a.command=="monitor":
            result=asyncio.run(s.rule(json.loads(a.rule_file.read_text(encoding="utf-8-sig"))))
        elif a.command=="status":
            result=s.status(a.id)
        elif a.command=="stop":
            result=s.stop(a.id)
        elif a.command=="verify":
            result=asyncio.run(s.verify(a.id))
        else:
            result=start_worker(s.store)
        output({"ok":True,"data":result})
    except KeyboardInterrupt:
        output({"ok":False,"code":"INTERRUPTED"})
        raise SystemExit(130)
    except Exception as e:
        error={"ok":False,"code":getattr(e,"code",type(e).__name__),"message":str(e)[:500]}
        if a.command=="mcp":
            print(json.dumps(error,ensure_ascii=False),file=sys.stderr)
        else:
            output(error)
        raise SystemExit(1)

