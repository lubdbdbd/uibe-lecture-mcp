"""Offline competition simulator. Never contacts a real school or uses a login."""
import argparse
import asyncio
from datetime import timedelta
import json
import gc
from pathlib import Path
import sys
import tempfile
import time

parser=argparse.ArgumentParser()
parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
parser.add_argument("--label",default="current")
parser.add_argument("--rtt-ms",type=float,default=80)
parser.add_argument("--sellout-ms",type=float,default=1000)
args=parser.parse_args()
sys.path.insert(0,str(args.root))
import httpx
from uibe_lecture.client import Client
from uibe_lecture.engine import Engine
from uibe_lecture.models import Lecture, Criteria, now
from uibe_lecture.storage import Store, TERMINAL


def page(state):
    text={"ready":"符合报名条件","full":"符合报名条件","enrolled":"您已报名该课程"}[state]
    quota=0 if state=="full" else 1
    return f'<form id="form1" action="/User/Mine/apply.html" method="POST">剩余名额：{quota}{text}<input name="classid" value="12345"><input name="SPAM" value="offline-only"><button id="submit1" lay-submit>提交</button></form>'


async def main():
    with tempfile.TemporaryDirectory(prefix="uibe-offline-benchmark-") as directory:
        store=Store(Path(directory)/"state.sqlite3")
        enrolled=set()
        submissions=[]
        ready_times={}
        started=time.perf_counter()
        async def handler(request):
            arrived=time.perf_counter()
            cid=(request.headers["referer"] if request.method=="POST" else request.url.path).split("/")[-1].split(".")[0]
            await asyncio.sleep(args.rtt_ms/1000)
            elapsed=(arrived-started)*1000
            if request.method=="POST":
                success=elapsed<=args.sellout_ms
                submissions.append({"course":cid,"sent_ms":round(elapsed,2),"ready_to_send_ms":round((arrived-ready_times[cid])*1000,2),"success":success})
                if success:
                    enrolled.add(cid)
                return httpx.Response(200,json={"status":1 if success else 0,"info":"报名成功" if success else "名额已满"})
            state="enrolled" if cid in enrolled else "full" if elapsed>args.sellout_ms else "ready"
            if state=="ready":
                ready_times[cid]=time.perf_counter()
            return httpx.Response(200,text=page(state))
        client=Client(store,transport=httpx.MockTransport(handler),session={"cookies":[]})
        engine=Engine(store,client)
        for cid in ("A","B","C"):
            current=now()
            course=Lecture(id=cid,title="模拟课程"+cid,start=current+timedelta(days=2),end=current+timedelta(days=2,hours=1),
                           opens=current-timedelta(seconds=1),closes=current+timedelta(days=1),credits=.1,
                           detail_url=f"https://ea.uibe.edu.cn/User/Class/viewclass/classid/{cid}.html",
                           apply_url=f"https://ea.uibe.edu.cn/User/Mine/apply/classid/{cid}.html")
            job=engine.enqueue(course,instruction="离线模拟",criteria=Criteria(ignore_conflicts=True))
            job["preflight_at"]=engine.clock()
            engine.save(job)
        started=time.perf_counter()
        async def pump():
            while any(j["state"] not in TERMINAL for j in store.all("job")):
                await engine.tick()
                await asyncio.sleep(.01 if hasattr(engine,"inflight") else .1)
            if getattr(engine,"inflight",None):
                await asyncio.gather(*engine.inflight.values())
        try:
            await asyncio.wait_for(pump(),15)
            print(json.dumps({"label":args.label,"rtt_ms":args.rtt_ms,"sellout_ms":args.sellout_ms,
                              "confirmed":sum(j["state"]=="confirmed" for j in store.all("job")),"courses":3,
                              "duration_ms":round((time.perf_counter()-started)*1000,2),"submissions":submissions},ensure_ascii=False))
        finally:
            for task in getattr(engine,"inflight",{}).values():
                task.cancel()
            if getattr(engine,"inflight",None):
                await asyncio.gather(*engine.inflight.values(),return_exceptions=True)
            await client.close()
            gc.collect()


asyncio.run(main())
