from __future__ import annotations

from . import __version__

import asyncio
import json
import os
import time
import uuid
from datetime import timedelta
import httpx
from .client import Client
from .models import Lecture, Rule, Criteria, Busy, dt, now, matches, overlaps
from .parser import AuthRequired, RateLimited, SiteError, SubmissionCancelled, FormExpired
from .storage import Store, TERMINAL


class Engine:
    def __init__(self, store=None, client=None):
        self.store=store or Store()
        self.client=client or Client(self.store)
        self.owner=f"{os.getpid()}-{uuid.uuid4().hex}"
        # Ready forms contain transient tokens and must stay out of persisted jobs.
        self.prepared={}
        self.job_locks={}
        self.inflight={}
        self.background=None
        self.sync_clock()

    def sync_clock(self):
        self.school_clock=self.store.get("system","school_clock") or {}

    def clock_offset(self):
        sample=self.school_clock
        return sample.get("offset_seconds",0) if time.time()-sample.get("checked_at",0)<1800 else 0

    def clock(self):
        return time.time()+self.clock_offset()

    def now(self):
        return now()+timedelta(seconds=self.clock_offset())

    def save(self, job, state=None, message=None, due=None):
        # Preserve an independently requested cancellation across an in-flight GET.
        live=self.store.get("job",job["id"])
        if live and live.get("cancel_requested"):
            job["cancel_requested"]=True
        if state:
            job["state"]=state
        if message is not None:
            job["message"]=message
        if due is not None:
            job["due"]=due
        job["updated"]=self.clock()
        self.store.put("job",job["id"],job)
        if state and (not live or live.get("state")!=state or live.get("message")!=message):
            self.store.event(job["id"],state,message=message)
        return job

    def enqueue(self, c: Lecture, *, instruction, watch_minutes=0, rule_id=None, criteria=None, dry_run=False, expires=None):
        if not instruction.strip():
            raise ValueError("须记录用户给出的具体报名指令")
        if not 0<=watch_minutes<=1440:
            raise ValueError("捡漏时间须在 0–1440 分钟内")
        with self.store.locked("enqueue"):
            for j in self.store.all("job"):
                if j["lecture"]["id"]==c.id and j["state"] not in TERMINAL:
                    return j
                if j["lecture"]["id"]==c.id and j["state"] in {"confirmed","already_enrolled"}:
                    return j
                if j["lecture"]["id"]==c.id and j["state"] in {"unconfirmed","interaction_required"}:
                    raise ValueError("已有待核实任务，请先核实结果再安排")
            deadline=min(c.closes, c.start, max(self.now(),c.opens)+timedelta(minutes=watch_minutes,seconds=30))
            if expires:
                deadline=min(deadline,dt(expires))
            if deadline<=self.now():
                raise ValueError("课程报名已截止或活动已开始")
            jid=uuid.uuid4().hex[:12]
            job={"id":jid,"lecture":c.model_dump(mode="json"),"instruction":instruction,"rule_id":rule_id,
                 "criteria":(criteria or Criteria()).model_dump(mode="json"),"state":"queued","due":self.clock(),
                 "deadline":deadline.isoformat(),"watch_minutes":watch_minutes,"attempts":0,"errors":0,
                 "dry_run":dry_run,"created":self.clock(),"cancel_requested":False}
            return self.save(job,"queued","已进入后台队列")

    def add_rule(self, rule: Rule):
        if rule.expires<=self.now():
            raise ValueError("监测结束时间必须在未来")
        rid=uuid.uuid4().hex[:12]
        obj={"id":rid,"rule":rule.model_dump(mode="json"),"enabled":True,"due":self.clock(),"created":self.clock()}
        self.store.put("rule",rid,obj)
        return obj

    async def discover(self, item):
        rule=Rule.model_validate(item["rule"])
        if rule.expires<=self.now():
            item["enabled"]=False
            self.store.put("rule",item["id"],item)
            return
        catalog=await self.client.lectures()
        candidates=[c for c in catalog if c.closes>self.now() and c.start>self.now() and matches(c,rule.criteria)]
        self.store.put("candidates",item["id"],{"at":self.clock(),"lectures":[c.model_dump(mode="json") for c in candidates]})
        if rule.mode=="enroll":
            jobs=[j for j in self.store.all("job") if j.get("rule_id")==item["id"]]
            committed=[j for j in jobs if j["state"] not in TERMINAL or j["state"] in {"confirmed","unconfirmed","interaction_required"}]
            slots=rule.max_success-len(committed)
            seen={j["lecture"]["id"] for j in jobs}
            for c in candidates:
                if slots<=0:
                    break
                if c.id in seen or (not rule.criteria.ignore_conflicts and any(overlaps(c,Lecture.model_validate(j["lecture"])) for j in committed)):
                    continue
                j=self.enqueue(c,instruction=rule.instruction,watch_minutes=rule.watch_minutes,rule_id=item["id"],
                               criteria=rule.criteria,expires=rule.expires)
                committed.append(j)
                slots-=1
        # Respect cancellation made while fetching the catalog.
        latest=self.store.get("rule",item["id"])
        item["enabled"]=latest.get("enabled",False) if latest else False
        item["due"]=self.clock()+rule.scan_seconds
        item.pop("error",None)
        self.store.put("rule",item["id"],item)

    async def preflight(self, j):
        c=Lecture.model_validate(j["lecture"])
        # A newly scheduled imminent job uses its cached course and live form.
        fresh=next((x for x in await self.client.lectures() if x.id==c.id),None) if c.opens.timestamp()-self.clock()>60 else None
        if fresh:
            c=fresh
            j["lecture"]=c.model_dump(mode="json")
            # Follow a changed opening time while retaining the user's expiry.
            if not j.get("preflight_at"):
                j["user_deadline"]=j["deadline"]
            deadline=min(c.closes,c.start,max(self.now(),c.opens)+timedelta(minutes=j["watch_minutes"],seconds=30))
            if j.get("rule_id"):
                r=self.store.get("rule",j["rule_id"])
                if r:
                    deadline=min(deadline,dt(r["rule"]["expires"]))
            j["deadline"]=deadline.isoformat()
        if not matches(c,Criteria.model_validate(j["criteria"])):
            return self.save(j,"ineligible","课程更新后不再满足筛选条件")
        state=await self.client.apply(c.id,fast=c.opens.timestamp()-self.clock()<=60)
        if state.state=="enrolled":
            return self.save(j,"already_enrolled",state.message)
        if state.state in {"closed","ineligible","interaction_required"}:
            return self.save(j,state.state,state.message)
        ignore_conflicts=Criteria.model_validate(j["criteria"]).ignore_conflicts
        reservations=[] if ignore_conflicts else await self.client.reservations(periods=True)
        conflicts=[r["title"] for r in reservations if overlaps(c,Busy.model_validate(r["period"]))]
        conflicts.extend(x["lecture"]["title"] for x in self.store.all("job")
                         if not ignore_conflicts and x["id"]!=j["id"] and x["state"] in {"confirmed","already_enrolled","submitting","verifying","unconfirmed"}
                         and overlaps(c,Lecture.model_validate(x["lecture"])))
        if conflicts:
            return self.save(j,"conflict","时间冲突："+"；".join(dict.fromkeys(conflicts)))
        j["preflight_at"]=self.clock()
        if j["dry_run"]:
            return self.save(j,"dry_run",f"预检通过，页面状态：{state.state}；未提交")
        if state.state=="ready":
            self.prepared[j["id"]]=(time.monotonic(),state)
        return self.save(j,"waiting","预检通过，等待开放",max(self.clock(),min(self.clock()+300,c.opens.timestamp()-60)))

    async def verify(self, j):
        cid=j["lecture"]["id"]
        j.setdefault("verify_started_at",self.clock())
        last=None
        for delay in (0,0.5,1):
            if delay:
                await asyncio.sleep(delay)
            try:
                last=await self.client.apply(cid,fast=True)
                if last.state=="enrolled":
                    return self.save(j,"confirmed","目标课程页面已确认：您已报名该课程")
            except (httpx.HTTPError, SiteError):
                continue
        if self.clock()-j["verify_started_at"]<120:
            j["verify_rounds"]=j.get("verify_rounds",0)+1
            return self.save(j,"verifying","提交已发出，继续只读核实报名结果",self.clock()+min(5,j["verify_rounds"]))
        return self.save(j,"unconfirmed","提交结果待核实；任务已停止提交")

    async def step(self, j):
        lock=self.job_locks.setdefault(j["id"],asyncio.Lock())
        if lock.locked():
            return self.store.get("job",j["id"])
        async with lock:
            return await self._step(self.store.get("job",j["id"]) or j)

    async def _step(self, j):
        if j["state"] in TERMINAL:
            return j
        # A crash after the write-ahead marker must never cause a blind resubmit.
        if j["state"] in {"submitting","verifying"}:
            return await self.verify(j)
        if j.get("cancel_requested"):
            return self.save(j,"cancelled","已停止后续报名请求")
        if j.get("rule_id"):
            r=self.store.get("rule",j["rule_id"])
            if not r or not r["enabled"] or dt(r["rule"]["expires"])<=self.now():
                return self.save(j,"cancelled","监测规则已停用或到期")
        c=Lecture.model_validate(j["lecture"])
        if self.now()>=dt(j["deadline"]):
            return self.save(j,"closed","已到任务截止时间")
        if not j.get("preflight_at") or (c.opens.timestamp()-self.clock()>60 and self.clock()-j["preflight_at"]>=300):
            checked=await self.preflight(j)
            if checked["state"]=="waiting" and c.opens.timestamp()-self.clock()<=5 and j["id"] in self.prepared:
                return await self._step(checked)
            return checked
        until=c.opens.timestamp()-self.clock()
        if until>60:
            return self.save(j,due=min(self.clock()+300,c.opens.timestamp()-60))
        fast=-20<until<=5
        prepared=self.prepared.pop(j["id"],None)
        form=prepared[1] if prepared and time.monotonic()-prepared[0]<=3 else await self.client.apply(c.id,fast=fast)
        if form.state=="enrolled":
            return self.save(j,"already_enrolled",form.message)
        if form.state in {"closed","ineligible","interaction_required"}:
            return self.save(j,form.state,form.message)
        if form.state=="unknown":
            raise SiteError("页面状态暂未识别："+form.message)
        if form.state=="not_open":
            delay=0.005 if fast else min(5,max(0.005,until-5)) if until>5 else 2
            return self.save(j,"waiting","报名尚未开放",self.clock()+delay)
        if form.state=="full":
            if j["watch_minutes"]<=0 and self.now()>=c.opens:
                return self.save(j,"full","名额已满")
            return self.save(j,"watching","名额已满，继续等待空位",self.clock()+(0.005 if fast else 2))
        if form.state!="ready":
            return self.save(j,"failed","不支持的报名状态")
        if until>5:
            self.prepared[j["id"]]=(time.monotonic(),form)
            return self.save(j,"waiting","报名页已预热，等待开抢窗口",max(self.clock()+0.005,c.opens.timestamp()-5))
        # Recheck cancellation and other confirmed jobs just before committing.
        latest=self.store.get("job",j["id"])
        if latest and latest.get("cancel_requested"):
            return self.save(latest,"cancelled","已停止后续报名请求")
        for other in ([] if Criteria.model_validate(j["criteria"]).ignore_conflicts else self.store.all("job")):
            if other["id"]!=j["id"] and other["state"] in {"confirmed","already_enrolled","submitting","verifying","unconfirmed"} and overlaps(c,Lecture.model_validate(other["lecture"])):
                return self.save(j,"conflict","与另一报名任务时间冲突")
        def before_send():
            # Run after the priority gate, immediately before the actual POST.
            # One transaction combines the final checks and durable submit marker.
            with self.store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row=connection.execute("SELECT body FROM objects WHERE kind='job' AND id=?",(j["id"],)).fetchone()
                latest=json.loads(row[0]) if row else None
                if latest and latest.get("cancel_requested"):
                    raise SubmissionCancelled("已停止尚未发出的报名")
                if latest and latest["state"] in TERMINAL|{"submitting"}:
                    raise SubmissionCancelled("该任务已有提交或已结束")
                if self.now()>=dt(j["deadline"]):
                    raise SubmissionCancelled("报名任务已到截止时间")
                if j.get("rule_id"):
                    row=connection.execute("SELECT body FROM objects WHERE kind='rule' AND id=?",(j["rule_id"],)).fetchone()
                    rule=json.loads(row[0]) if row else None
                    if not rule or not rule["enabled"] or dt(rule["rule"]["expires"])<=self.now():
                        raise SubmissionCancelled("监测规则已停用或到期")
                if not Criteria.model_validate(j["criteria"]).ignore_conflicts:
                    for row in connection.execute("SELECT body FROM objects WHERE kind='job'"):
                        other=json.loads(row[0])
                        if other["id"]!=j["id"] and other["state"] in {"confirmed","already_enrolled","submitting","verifying","unconfirmed"} and overlaps(c,Lecture.model_validate(other["lecture"])):
                            raise SubmissionCancelled("另一场报名与本场时间冲突")
                j["attempts"]+=1
                j.update(state="submitting",message="已记录提交意图，正在发送",updated=self.clock())
                connection.execute("INSERT OR REPLACE INTO objects VALUES('job',?,?)",(j["id"],json.dumps(j,ensure_ascii=False)))
                timing={"opening_delta_ms":round((self.clock()-c.opens.timestamp())*1000,2)}
                connection.execute("INSERT INTO events(at,job,event,body) VALUES(?,?,?,?)",(time.time(),j["id"],"submit_timing",json.dumps(timing)))
        try:
            reply=await self.client.submit(c.id,form,before_send=before_send)
            self.store.event(j["id"],"server_reply",**reply)
        except SubmissionCancelled as exc:
            return self.save(j,"cancelled",str(exc))
        except FormExpired as exc:
            return self.save(j,"waiting",str(exc),self.clock()+0.005)
        except Exception as exc:
            # Even a timeout/429 may follow a server-side commit.
            self.store.event(j["id"],"submit_error",error=type(exc).__name__)
            return await self.verify(j)
        # Only explicit rejections permit another fresh form. Unknown responses stop.
        message=reply["message"]
        if reply.get("status") in (0,"0",False) and any(x in message for x in ("未开始","名额已满","名额不足","已满员")):
            if "未开始" in message and self.now()<dt(j["deadline"]):
                return self.save(j,"waiting",message,self.clock()+0.005)
            if j["watch_minutes"]>0 and self.now()<dt(j["deadline"]):
                return self.save(j,"watching",message,self.clock()+(0.005 if fast else 2))
            return self.save(j,"full",message)
        return await self.verify(j)

    async def keep_session_alive(self):
        status=self.store.get("system","session_health") or {}
        if self.clock()-status.get("checked_at",0)<600:
            return
        status={"checked_at":self.clock()}
        try:
            await self.client.request("GET","/User/Mine/index.html")
            status["logged_in"]=True
        except Exception as exc:
            status.update(logged_in=False,error=type(exc).__name__)
        self.store.put("system","session_health",status)

    async def execute_job(self, j):
        try:
            result=await self.step(j)
            if result and result.get("errors"):
                result["errors"]=0
                self.save(result)
        except AuthRequired as exc:
            j=self.store.get("job",j["id"]) or j
            self.save(j,"auth_required",str(exc),self.clock()+5)
        except RateLimited as exc:
            j=self.store.get("job",j["id"]) or j
            self.save(j,"backoff",str(exc),self.clock()+exc.seconds)
        except (httpx.HTTPError,SiteError) as exc:
            j=self.store.get("job",j["id"]) or j
            j["errors"]=j.get("errors",0)+1
            until=dt(j["lecture"]["opens"]).timestamp()-self.clock()
            delay=0.1 if -20<until<=5 else min(60,2**min(j["errors"],6))
            retry_at=self.clock()+delay
            if until>5:
                retry_at=min(retry_at,dt(j["lecture"]["opens"]).timestamp()-5)
            self.save(j,"backoff",type(exc).__name__,retry_at)
        except Exception as exc:
            j=self.store.get("job",j["id"]) or j
            self.save(j,"failed",str(exc)[:300])

    async def tick(self):
        self.sync_clock()
        if self.background and self.background.done():
            self.background.result()
            self.background=None
        for ident,task in list(self.inflight.items()):
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                del self.inflight[ident]
        active=[j for j in self.store.all("job") if j["state"] not in TERMINAL]
        imminent=[j for j in active if -20<dt(j["lecture"]["opens"]).timestamp()-self.clock()<=60]
        jobs=[j for j in active if j.get("due",0)<=self.clock() and j["id"] not in self.inflight]
        if imminent:
            imminent_ids={j["id"] for j in imminent}
            jobs=[j for j in jobs if j["id"] in imminent_ids or j["state"] in {"submitting","verifying"}]
        jobs.sort(key=lambda j:(0 if "先修" in j["lecture"].get("category","")+j["lecture"].get("title","") else 1,
                                j.get("due",0),j["created"]))
        if jobs:
            for j in jobs[:max(0,8-len(self.inflight))]:
                self.inflight[j["id"]]=asyncio.create_task(self.execute_job(j))
            await asyncio.sleep(0)
            return
        if imminent or self.inflight or self.background:
            return
        self.background=asyncio.create_task(self.maintenance())

    async def maintenance(self):
        for r in self.store.all("rule"):
            if r["enabled"] and r.get("due",0)<=self.clock():
                try:
                    await self.discover(r)
                except Exception as e:
                    r["error"]=str(e)[:300]
                    r["due"]=self.clock()+max(60,getattr(e,"seconds",60))
                    self.store.put("rule",r["id"],r)
                return

        await self.keep_session_alive()

    async def run(self):
        # OS-held file lock prevents two workers, including across crashes.
        lock_path=self.store.path.parent/"worker.lock"
        lock=lock_path.open("a+b")
        try:
            lock.seek(0)
            if os.name=="nt":
                import msvcrt
                if lock_path.stat().st_size==0:
                    lock.write(b"0"); lock.flush(); lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,IOError):
            lock.close()
            return
        async def heartbeat():
            while True:
                health={"pid":os.getpid(),"heartbeat":time.time(),"school_clock_offset_seconds":round(self.clock_offset(),3),
                        "version":__version__,"concurrent_jobs":len(self.inflight),
                        "clock_uncertainty_seconds":round(self.school_clock.get("uncertainty_seconds",1),3)}
                if os.name=="nt":
                    import ctypes
                    active=any(j["state"] not in TERMINAL for j in self.store.all("job"))
                    # Keep the system awake only while accepted jobs are pending.
                    result=ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if active else 0x80000000)
                    health["preventing_sleep"]=bool(result) and active
                self.store.put("system","worker",health)
                await asyncio.sleep(2)
        pulse=asyncio.create_task(heartbeat())
        try:
            while True:
                await self.tick()
                armed=any(j["state"] not in TERMINAL and -20<dt(j["lecture"]["opens"]).timestamp()-self.clock()<=5 for j in self.store.all("job"))
                await asyncio.sleep(0.01 if armed else 0.1)
        finally:
            if self.background:
                self.background.cancel()
                await asyncio.gather(self.background,return_exceptions=True)
            for task in self.inflight.values():
                task.cancel()
            if self.inflight:
                await asyncio.gather(*self.inflight.values(),return_exceptions=True)
            pulse.cancel()
            try:
                await pulse
            except asyncio.CancelledError:
                pass
            await self.client.close()
            if os.name=="nt":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            lock.close()
