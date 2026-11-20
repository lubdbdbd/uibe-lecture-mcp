from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from .models import Criteria, Lecture, Rule, matches
from .storage import Store, TERMINAL, data_dir
from .client import Client
from .parser import AuthRequired
from .engine import Engine

ROOT=Path(__file__).resolve().parent.parent


def worker_lock_held(store):
    """The OS lock is authoritative; a recent heartbeat can outlive a crash."""
    from .auth import file_lock
    try:
        with file_lock(store.path.parent / "worker.lock"):
            return False
    except OSError:
        return True


def start_worker(store):
    from .auth import file_lock
    # Serialize simultaneous CLI/MCP/watchdog starts without touching job state.
    with file_lock(store.path.parent / "worker-start.lock", blocking=True):
        status = store.get("system", "worker") or {}
        if worker_lock_held(store):
            return {**status, "started": False, "running": True,
                    "healthy": time.time() - status.get("heartbeat", 0) < 10}
        launching = store.get("system", "worker_launch") or {}
        if time.time() - launching.get("at", 0) < 10:
            return {"started": False, "starting": True, "pid": launching.get("pid")}
        cmd = [sys.executable, "-m", "uibe_lecture", "worker"]
        env = dict(os.environ, UIBE_DATA_DIR=str(store.path.parent))
        with (store.path.parent / "worker.log").open("ab") as log:
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                     subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, creationflags=flags,
                                    start_new_session=os.name != "nt")
        store.put("system", "worker_launch", {"pid": proc.pid, "at": time.time()})
        return {"started": True, "pid": proc.pid}


def spawn_login():
    store=Store()
    existing=store.get("system","login") or {}
    if existing.get("state") in {"starting","window_open"} and time.time()-existing.get("at",0)<600:
        return {"login_pid":existing["pid"],"state":existing["state"],"message":"登录窗口已在处理，请完成该窗口中的学校登录"}
    cmd=[sys.executable,"-m","uibe_lecture","auth","login"]
    env=dict(os.environ,UIBE_DATA_DIR=str(store.path.parent))
    p=subprocess.Popen(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=="nt" else 0,
        start_new_session=os.name!="nt")
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        state=store.get("system","login") or {}
        if state.get("pid")==p.pid:
            if state.get("state")=="saved":
                return {"login_pid":p.pid,"state":"saved","message":"已有会话已恢复并保存，无需重复输入"}
            if state.get("state")=="window_open":
                return {"login_pid":p.pid,"state":"window_open","message":"已确认学校浏览器窗口创建，请在窗口内登录；成功后会持久保存"}
            if state.get("state")=="failed":
                return {"login_pid":p.pid,"state":"failed","message":state.get("message"),"error":state.get("error")}
        if p.poll() is not None:
            return {"login_pid":p.pid,"state":"failed","message":"登录进程已退出，未确认窗口打开","exit_code":p.returncode}
        time.sleep(.1)
    return {"login_pid":p.pid,"state":"starting","message":"登录浏览器仍在启动，尚未确认窗口打开"}


class Service:
    def __init__(self):
        self.store=Store()

    async def catalog(self, criteria=None, refresh=True):
        f=Criteria.model_validate(criteria or {})
        client=Client(self.store)
        try:
            rows=await client.lectures() if refresh else [Lecture.model_validate(x) for x in self.store.all("lecture")]
            return [c.model_dump(mode="json") for c in rows if matches(c,f)]
        finally:
            await client.close()

    async def get_lecture(self, cid):
        obj=self.store.get("lecture",cid)
        if not obj:
            await self.catalog()
            obj=self.store.get("lecture",cid)
        if not obj:
            raise ValueError("未在当前课程列表找到该 ID")
        return Lecture.model_validate(obj)

    async def details(self, cid):
        c=await self.get_lecture(cid)
        client=Client(self.store)
        try:
            return await client.details(c)
        finally:
            await client.close()

    async def reservations(self):
        client=Client(self.store)
        try:
            return await client.reservations(periods=True)
        finally:
            await client.close()

    async def auth_status(self):
        client=Client(self.store)
        try:
            await client.reservations()
            return {"logged_in":True,"session_persisted":True}
        except AuthRequired as exc:
            return {"logged_in":False,"needs_login":True,"message":str(exc),"login":self.store.get("system","login")}
        finally:
            await client.close()

    async def reserve(self,cid,instruction,watch_minutes=0,criteria=None,dry_run=False):
        c=await self.get_lecture(cid)
        e=Engine(self.store)
        try:
            j=e.enqueue(c,instruction=instruction,watch_minutes=watch_minutes,criteria=Criteria.model_validate(criteria or {}),dry_run=dry_run)
            return {"job":j,"worker":start_worker(self.store)}
        finally:
            await e.client.close()

    async def rule(self, rule):
        e=Engine(self.store)
        try:
            item=e.add_rule(Rule.model_validate(rule))
            return {"rule":item,"worker":start_worker(self.store)}
        finally:
            await e.client.close()

    def status(self, job_id=None):
        w=self.store.get("system","worker") or {}
        return {"worker":dict(w,alive=time.time()-w.get("heartbeat",0)<60),
                "jobs":[self.store.get("job",job_id)] if job_id else self.store.all("job"),
                "rules":self.store.all("rule"),"candidates":self.store.all("candidates"),
                "events":self.store.events(job_id)}

    def stop(self, ident):
        j=self.store.get("job",ident)
        if j:
            j["cancel_requested"]=True
            j["due"]=time.time()
            self.store.put("job",ident,j)
            return {"cancel_requested":True,"id":ident,"message":"停止尚未发出的请求；已发出的提交会继续核实结果"}
        r=self.store.get("rule",ident)
        if r:
            r["enabled"]=False
            self.store.put("rule",ident,r)
            for j in self.store.all("job"):
                if j.get("rule_id")==ident and j["state"] not in TERMINAL:
                    j["cancel_requested"]=True
                    j["due"]=time.time()
                    self.store.put("job",j["id"],j)
            return {"stopped":True,"id":ident}
        raise ValueError("找不到任务或规则 ID")

    async def verify(self, job_id):
        j=self.store.get("job",job_id)
        if not j:
            raise ValueError("找不到任务 ID")
        if j["state"] not in TERMINAL:
            raise ValueError("任务仍在运行，请先读取任务状态")
        client=Client(self.store)
        try:
            form=await client.apply(j["lecture"]["id"])
            if form.state=="enrolled":
                j["state"]="confirmed"
                j["message"]="目标课程页面已确认：您已报名该课程"
                self.store.put("job",job_id,j)
            return {"job":j,"website_state":form.state,"message":form.message}
        finally:
            await client.close()

