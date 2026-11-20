import asyncio
import time
from email.utils import formatdate
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

import uibe_lecture.engine as engine_module
from uibe_lecture.client import Client
from uibe_lecture.engine import Engine
from uibe_lecture.models import Criteria, now
from test_core import Campus, lecture, store


def test_school_clock_allows_opening_when_local_clock_is_slow(tmp_path):
    async def run():
        s=store(tmp_path)
        fake=Campus()
        fake.c=lecture(opens=now()+timedelta(seconds=5))
        def handler(req):
            response=fake(req)
            response.headers["Date"]=formatdate(time.time()+10, usegmt=True)
            return response
        client=Client(s, transport=httpx.MockTransport(handler), session={"cookies": []})
        await client.request("GET", "/User/Mine/index.html")
        e=Engine(s, client)
        assert 9<e.clock()-time.time()<11
        j=e.enqueue(fake.c, instruction="按学校时间准点提交")
        j["preflight_at"]=e.clock()
        e.save(j)
        assert (await e.step(j))["state"]=="confirmed"
        assert fake.posts==1
        await client.close()
    asyncio.run(run())


def test_expired_school_clock_is_ignored(tmp_path):
    s=store(tmp_path)
    s.put("system", "school_clock", {"checked_at":time.time()-1801,"offset_seconds":100})
    e=Engine(s)
    assert abs(e.clock()-time.time())<0.1
    asyncio.run(e.client.close())


def test_warm_ready_form_submits_without_extra_get(tmp_path):
    async def run():
        s=store(tmp_path)
        fake=Campus()
        base=now()
        fake.c=lecture(opens=base+timedelta(seconds=6))
        clock=[base]
        client=Client(s, transport=httpx.MockTransport(fake), session={"cookies": []})
        e=Engine(s, client)
        e.clock=lambda:clock[0].timestamp()
        e.now=lambda:clock[0]
        j=e.enqueue(fake.c, instruction="抢此课程")
        j["preflight_at"]=base.timestamp()
        e.save(j)
        assert (await e.step(j))["state"]=="waiting"
        assert fake.posts==0
        assert "form-token" not in str(s.all("job"))
        fake.calls.clear()
        clock[0]=base+timedelta(seconds=1)
        assert (await e.step(j))["state"]=="confirmed"
        assert fake.calls[0][0]=="POST"
        assert fake.posts==1
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("ignore", [False, True])
def test_conflict_policy_applies_to_preflight_and_submission(tmp_path, ignore):
    async def run():
        s=store(tmp_path)
        fake=Campus()
        client=Client(s, transport=httpx.MockTransport(fake), session={"cookies": []})
        e=Engine(s, client)
        other=e.enqueue(lecture(id="other"), instruction="已有目标")
        e.save(other, "confirmed")
        j=e.enqueue(fake.c, instruction="本场不考虑冲突", criteria=Criteria(ignore_conflicts=ignore))
        out=await e.preflight(j)
        if ignore:
            assert out["state"]=="waiting"
            assert (await e.step(j))["state"]=="confirmed"
            assert fake.posts==1
        else:
            assert out["state"]=="conflict"
            assert fake.posts==0
        await client.close()
    asyncio.run(run())


def test_connection_failure_does_not_kill_future_job_and_resets(tmp_path):
    async def run():
        s=store(tmp_path)
        e=Engine(s)
        j=e.enqueue(lecture(opens=now()+timedelta(hours=1)), instruction="等待开放")
        j["errors"]=7
        e.save(j)
        async def broken(job):
            raise httpx.ConnectError("temporary outage")
        e.step=broken
        await e.tick()
        j=s.get("job", j["id"])
        assert j["state"]=="backoff" and j["errors"]==8
        assert j["due"]>now().timestamp()
        j["due"]=0
        s.put("job", j["id"], j)
        async def recovered(job):
            return e.save(job, "waiting", due=now().timestamp()+300)
        e.step=recovered
        await e.tick()
        assert s.get("job", j["id"])["errors"]==0
        await e.client.close()
    asyncio.run(run())


def test_other_catalog_work_does_not_block_imminent_opening(tmp_path):
    async def run():
        s=store(tmp_path)
        e=Engine(s)
        ready=e.enqueue(lecture(opens=now()+timedelta(seconds=10)), instruction="准点抢")
        e.save(ready, due=now().timestamp()+8)
        e.enqueue(lecture(id="later", opens=now()+timedelta(hours=1)), instruction="稍后")
        calls=[]
        async def record(job):
            calls.append(job["id"])
        e.step=record
        await e.tick()
        assert calls==[]
        await e.client.close()
    asyncio.run(run())
