import asyncio
import time
import sqlite3
from datetime import timedelta

import httpx

from uibe_lecture.client import Client
from uibe_lecture.engine import Engine
from uibe_lecture.models import Criteria, now
from uibe_lecture.pacing import RequestGate
from uibe_lecture.storage import Store
from test_core import Campus, form, lecture, store


def test_submit_preempts_queued_refreshes(tmp_path):
    async def run():
        class Slots:
            polls_open=False
            def claim_request_slot(self, interval, critical=False):
                return 0 if critical or self.polls_open else 60
        slots=Slots()
        gate=RequestGate(slots)
        order=[]
        async def poll():
            await gate.acquire(.25,urgent=True)
            order.append("poll")
        queued=asyncio.create_task(poll())
        await asyncio.sleep(0)
        await asyncio.wait_for(gate.acquire(.25,critical=True),.5)
        order.append("submit")
        slots.polls_open=True
        gate.wake()
        await queued
        assert order==["submit","poll"]
    asyncio.run(run())


def test_global_cooldown_applies_to_submission_and_refresh(tmp_path):
    s=Store(tmp_path/"state.sqlite3")
    assert s.claim_request_slot(.25)==0
    assert s.claim_request_slot(.25)>0
    s.defer_requests(2)
    assert s.claim_request_slot(.25,critical=True)>1.5
    assert s.claim_request_slot(.25)>1.5
    assert Store(s.path).get("system","absent") is None


def test_database_connections_close_after_transaction(tmp_path):
    s=Store(tmp_path/"state.sqlite3")
    with s.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0]==1
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("database connection leaked")


def test_unknown_submission_keeps_verifying_without_reposting(tmp_path):
    async def run():
        s=store(tmp_path)
        campus=Campus("timeout_unknown")
        c=Client(s,transport=httpx.MockTransport(campus),session={"cookies":[]})
        e=Engine(s,c)
        j=e.enqueue(campus.c,instruction="提交后持续核实")
        j["preflight_at"]=e.clock()
        e.save(j)
        result=await e.step(j)
        assert result["state"]=="verifying" and campus.posts==1
        campus.enrolled=True
        result=await e.step(result)
        assert result["state"]=="confirmed" and campus.posts==1
        await c.close()
    asyncio.run(run())


def test_confirmed_course_is_not_enqueued_again(tmp_path):
    s=store(tmp_path)
    e=Engine(s)
    course=lecture()
    j=e.enqueue(course,instruction="原任务")
    e.save(j,"confirmed")
    assert e.enqueue(course,instruction="重复指令")["id"]==j["id"]
    assert len(s.all("job"))==1
    asyncio.run(e.client.close())


def test_slow_future_preflight_does_not_hold_scheduler(tmp_path):
    async def run():
        s=store(tmp_path)
        e=Engine(s)
        blocked=asyncio.Event()
        release=asyncio.Event()
        imminent_done=asyncio.Event()
        future=e.enqueue(lecture(id="future",opens=now()+timedelta(hours=1)),instruction="较晚课程")
        async def controlled(job):
            if job["id"]==future["id"]:
                blocked.set()
                await release.wait()
            else:
                imminent_done.set()
        e.step=controlled
        await asyncio.wait_for(e.tick(),.5)
        await blocked.wait()
        e.enqueue(lecture(id="urgent"),instruction="现在开抢")
        await e.tick()
        await asyncio.wait_for(imminent_done.wait(),.5)
        release.set()
        await asyncio.gather(*e.inflight.values())
        await e.client.close()
    asyncio.run(run())


def test_simultaneous_preflights_share_one_catalog_fetch(tmp_path):
    async def run():
        s=store(tmp_path)
        campus=Campus()
        async def handler(req):
            await asyncio.sleep(.01)
            return campus(req)
        c=Client(s,transport=httpx.MockTransport(handler),session={"cookies":[]})
        first,second=await asyncio.gather(c.lectures(),c.lectures())
        assert [x.id for x in first]==[x.id for x in second]
        assert sum("/Class/index" in path for _,path in campus.calls)==2
        await c.close()
    asyncio.run(run())


def test_same_job_has_only_one_inflight_submission(tmp_path):
    async def run():
        s=store(tmp_path)
        campus=Campus()
        async def handler(req):
            await asyncio.sleep(.01)
            return campus(req)
        c=Client(s,transport=httpx.MockTransport(handler),session={"cookies":[]})
        e=Engine(s,c)
        j=e.enqueue(campus.c,instruction="只报名一次")
        j["preflight_at"]=e.clock()
        e.save(j)
        await asyncio.gather(e.step(j),e.step(j))
        assert campus.posts==1
        assert s.get("job",j["id"])["state"]=="confirmed"
        await c.close()
    asyncio.run(run())


def test_cancellation_while_submit_waits_for_gate_sends_no_post(tmp_path):
    async def run():
        s=store(tmp_path)
        campus=Campus()
        c=Client(s,transport=httpx.MockTransport(campus),session={"cookies":[]})
        e=Engine(s,c)
        j=e.enqueue(campus.c,instruction="取消排队中的提交")
        j["preflight_at"]=e.clock()
        e.save(j)
        entered=asyncio.Event()
        release=asyncio.Event()
        async def gate(interval,critical=False,urgent=False):
            if critical:
                entered.set()
                await release.wait()
        c.gate.acquire=gate
        pending=asyncio.create_task(e.step(j))
        await entered.wait()
        latest=s.get("job",j["id"])
        latest["cancel_requested"]=True
        s.put("job",j["id"],latest)
        release.set()
        assert (await pending)["state"]=="cancelled"
        assert campus.posts==0
        await c.close()
    asyncio.run(run())


def test_rotated_login_refetches_form_without_post(tmp_path):
    async def run():
        s=store(tmp_path)
        campus=Campus()
        c=Client(s,transport=httpx.MockTransport(campus),session={"cookies":[]})
        e=Engine(s,c)
        j=e.enqueue(campus.c,instruction="按最新会话提交")
        j["preflight_at"]=e.clock()
        e.save(j)
        fresh=await c.apply(campus.c.id)
        e.prepared[j["id"]]=(time.monotonic(),fresh)
        c.sync_session=lambda:setattr(c,"version",2)
        result=await e.step(j)
        assert result["state"]=="waiting" and result["attempts"]==0
        assert campus.posts==0
        await c.close()
    asyncio.run(run())


def test_slow_verification_does_not_block_other_course(tmp_path):
    async def run():
        s=store(tmp_path)
        posted=set()
        release=asyncio.Event()
        second_sent=asyncio.Event()
        async def handler(req):
            if req.method=="POST":
                cid=req.headers["referer"].split("/")[-1].split(".")[0]
                posted.add(cid)
                if cid=="second":
                    second_sent.set()
                return httpx.Response(200,json={"status":1,"info":"报名成功"})
            cid=req.url.path.split("/")[-1].split(".")[0]
            if cid=="first" and cid in posted:
                await release.wait()
            return httpx.Response(200,text=form("enrolled" if cid in posted else "ready"))
        c=Client(s,transport=httpx.MockTransport(handler),session={"cookies":[]})
        e=Engine(s,c)
        jobs=[]
        for cid in ("first","second"):
            j=e.enqueue(lecture(id=cid),instruction="同时开抢",criteria=Criteria(ignore_conflicts=True))
            j["preflight_at"]=e.clock()
            e.save(j)
            jobs.append(j)
        await e.tick()
        await asyncio.wait_for(second_sent.wait(),.5)
        assert not release.is_set()
        release.set()
        await asyncio.gather(*e.inflight.values())
        assert all(s.get("job",j["id"])["state"]=="confirmed" for j in jobs)
        await c.close()
    asyncio.run(run())


def test_explicit_not_open_rejection_retries_without_verification_delay(tmp_path):
    async def run():
        s=store(tmp_path)
        reads=[]
        def handler(req):
            if req.method=="POST":
                return httpx.Response(200,json={"status":0,"info":"报名未开始"})
            reads.append(req.url.path)
            return httpx.Response(200,text=form())
        c=Client(s,transport=httpx.MockTransport(handler),session={"cookies":[]})
        e=Engine(s,c)
        j=e.enqueue(lecture(),instruction="按真实开放状态重试")
        j["preflight_at"]=e.clock()
        e.save(j)
        result=await e.step(j)
        assert result["state"]=="waiting" and result["attempts"]==1
        assert len(reads)==1
        assert result["due"]-e.clock()<.1
        await c.close()
    asyncio.run(run())
