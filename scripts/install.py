"""User-run installer: host-neutral local setup; Codex configuration is opt-in."""
from __future__ import annotations
import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

BEGIN="# BEGIN UIBE LECTURE MCP"
END="# END UIBE LECTURE MCP"


def update_config(original: str, python: Path, root: Path):
    parsed=tomllib.loads(original) if original.strip() else {}
    if "uibe-lecture" in parsed.get("mcp_servers",{}) and BEGIN not in original:
        raise ValueError("已有同名 uibe-lecture 配置，请先核对现有配置")
    quote=lambda s:json.dumps(str(s),ensure_ascii=False)
    block="\n".join([BEGIN,'[mcp_servers.uibe-lecture]',f'command = {quote(python)}',
        f'args = [{quote(root / "run.py")}, "mcp"]','startup_timeout_sec = 30','tool_timeout_sec = 180',
        '[mcp_servers.uibe-lecture.env]',f'UIBE_DATA_DIR = {quote(root / "data")}',END])
    if BEGIN in original:
        before,rest=original.split(BEGIN,1)
        if END not in rest:
            raise ValueError("已有配置标记不完整，请核对")
        _,after=rest.split(END,1)
        text=before+block+after
    else:
        text=original.rstrip()+"\n\n"+block+"\n"
    tomllib.loads(text)
    return text


def choose_codex(explicit=None, *, ask=input):
    if explicit is not None:
        return explicit
    if not sys.stdin.isatty():
        return False
    return ask("Configure Codex MCP too? [y/N]: ").strip().lower() in {"y", "yes"}


def connection_config(python, root):
    return {"mcpServers":{"uibe-lecture":{
        "command":str(python), "args":[str(root / "run.py"), "mcp"],
        "env":{"UIBE_DATA_DIR":str(root / "data")}}}}


def print_connection(python, root):
    print("Python:", python)
    print("CLI:", root / "run.py")
    print("Data:", root / "data")
    print("STDIO MCP configuration (local paths only; no credentials):")
    print(json.dumps(connection_config(python, root),ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--no-login",action="store_true")
    ap.add_argument("--configure-only",action="store_true",help="Use current source folder; host choice and setup flags still apply")
    choice=ap.add_mutually_exclusive_group()
    choice.add_argument("--codex",dest="codex",action="store_true",default=None)
    choice.add_argument("--no-codex",dest="codex",action="store_false")
    args=ap.parse_args()
    source=Path(__file__).resolve().parents[1]
    if os.name!="nt":
        raise SystemExit("此一键安装器用于 Windows；其他系统按 README 配置 MCP。")
    target=source if args.configure_only else Path(os.environ["LOCALAPPDATA"])/"UIBE-Lecture"
    target.mkdir(parents=True,exist_ok=True)
    if source.resolve()!=target.resolve():
        for folder in ("uibe_lecture","scripts","tests"):
            shutil.copytree(source/folder,target/folder,dirs_exist_ok=True,ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("run.py","pyproject.toml","README.md","AGENTS.md","install.cmd","login.cmd","status.cmd","example-rule.json"):
            shutil.copy2(source/name,target/name)
    modules=["httpx","bs4","mcp","playwright","socksio"]
    if not all(importlib.util.find_spec(x) for x in modules):
        subprocess.run([sys.executable,"-m","pip","install","httpx[socks]>=0.28,<0.29","beautifulsoup4>=4.12,<5","mcp>=1.20,<2","playwright>=1.50,<2"],check=True)
    if choose_codex(args.codex):
        config=Path(os.environ.get("CODEX_HOME",str(Path.home()/".codex")))/"config.toml"
        original=config.read_text(encoding="utf-8-sig") if config.exists() else ""
        updated=update_config(original,Path(sys.executable),target)
        if original!=updated:
            config.parent.mkdir(parents=True,exist_ok=True)
            if config.exists():
                shutil.copy2(config,config.with_name("config.toml.uibe-backup-"+datetime.now().strftime("%Y%m%d-%H%M%S")))
            temp=config.with_name("config.toml.uibe-new")
            temp.write_text(updated,encoding="utf-8")
            os.replace(temp,config)
        print("Codex MCP configured. Restart Codex to load it.")
    else:
        print("Codex config unchanged. Use the STDIO configuration in your host.")
    print_connection(Path(sys.executable),target)
    if not args.no_login:
        subprocess.run([sys.executable,str(target/"run.py"),"setup"],cwd=target,check=True)


if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("安装未完成：",e)
        raise SystemExit(1)

