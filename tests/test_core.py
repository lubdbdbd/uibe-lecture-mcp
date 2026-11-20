import asyncio
from datetime import timedelta
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from uibe_lecture.models import Lecture, Criteria, Rule, Busy, now, dt, matches
from uibe_lecture.parser import parse_apply, parse_lectures, parse_reservations, detail_period, SiteError, AuthRequired, RateLimited
from uibe_lecture.storage import Store
from uibe_lecture.client import Client
from uibe_lecture.engine import Engine


def lecture(**kw):
    n=now()
    vals=dict(id="test_A",title="AI 讲座",start=n+timedelta(days=2),end=n+timedelta(days=2,hours=1),
              opens=n-timedelta(seconds=1),closes=n+timedelta(days=1),credits=0.2,
              platform="校园文化平台",category="学术实践",organizer="测试学院",status="报名中",
              detail_url="https://ea.uibe.edu.cn/User/Class/viewclass/classid/test_A.html",
              apply_url="https://ea.uibe.edu.cn/User/Mine/apply/classid/test_A.html")
    vals.update(kw)
    return Lecture(**vals)


def form(state="ready",quota=2,field="12345",spam="form-token"):
    message={"ready":"符合报名条件","not_open":"报名未开始","enrolled":"您已报名该课程","closed":"报名已结束",
             "ineligible":"不符合报名条件","interaction_required":"请输入验证码"}[state]
    submit='<input name="classid" value="'+field+'"><button id="submit1" lay-submit>提交报名</button>' if state=="ready" else ""
    return f'<form id="form1" action="/User/Mine/apply.html" method="POST">剩余名额：{quota}<div>{message}</div>{submit}<input type="hidden" name="SPAM" value="{spam}"></form>'


def listing(c=None,next_page=""):
    c=c or lecture()
    def ts(d): return d.strftime("%Y-%m-%d %H:%M")
    return f'''<table><thead><tr><th>课程信息</th><th>活动时间</th><th>报名时间</th></tr></thead><tbody><tr>
    <td><a href="/User/Class/viewclass/classid/{c.id}.html">{c.title}</a><div class="text-muted">所属平台：{c.platform} / {c.category}</div></td>
    <td>{ts(c.start)}<br>{ts(c.end)}</td><td>{ts(c.opens)}<br>{ts(c.closes)}</td><td>{c.credits}</td><td>{c.organizer}</td><td>报名中</td>
    <td><a path="/User/Mine/apply/classid/{c.id}.html">报名</a></td></tr></tbody></table>{next_page}'''


def store(tmp_path):
    s=Store(tmp_path/"state.sqlite3")
    s.reserve_slot=lambda _:0
    s.claim_request_slot=lambda *args:0
    return s


def test_parse_real_structure():
    rows,pages=parse_lectures(listing(),"https://ea.uibe.edu.cn/User/Class/index.html?navid=1")
    assert len(rows)==1 and rows[0].platform=="校园文化平台"
    assert rows[0].start.utcoffset()==timedelta(hours=8)
    assert rows[0].category=="学术实践"


@pytest.mark.parametrize("state",["not_open","enrolled","ineligible","closed","interaction_required"])
def test_form_states(state):
    assert parse_apply(form(state),"test_A").state==state


def test_quota_zero_not_ready():
    assert parse_apply(form(quota=0),"test_A").state=="full"


def test_form_preserves_numeric_id_and_spam():
    f=parse_apply(form(),"test_A")
    assert f.fields=={"classid":"12345","SPAM":"form-token"}
    assert f.course_id=="test_A"
    assert "form-token" not in repr(f)


def test_real_form_duplicate_attributes_follow_browser():
    html=(Path(__file__).parent/"fixtures"/"apply_ready_real_structure.html").read_text(encoding="utf-8")
    result=parse_apply(html,"jP4fWvGJUd0")
    assert result.state=="ready"
    assert result.action=="https://ea.uibe.edu.cn/User/Mine/apply.html"
    assert result.fields["classid"]=="12345"
    assert result.fields["SPAM"]=="fixture-token-not-a-live-session"


def test_rejects_malformed_and_foreign_form():
    for bad in (form().replace('value="12345"','value=""'),form().replace('/User/Mine/apply.html','https://evil.test/collect'),"<html>unknown</html>"):
        with pytest.raises(SiteError):
            parse_apply(bad,"test_A")


def test_login_detection():
    with pytest.raises(AuthRequired):
        parse_apply('<input type="password">',"test_A")


def test_mine_does_not_use_mislabeled_times():
    html='<table><thead>课程名称 报名时间</thead><tr><td><a href="/User/Mine/bookdetail/id/x.html">测试讲座</a></td><td></td><td></td><td>2020-01-01 01:00</td><td>0.2</td><td>2026-11-10 12:00</td></tr></table>'
    rows,_=parse_reservations(html,"https://ea.uibe.edu.cn/User/Mine/index.html")
    assert "start" not in rows[0]
    detail='<div><h5>活动时间</h5><p>2026-11-12 15:30 - 2026-11-12 17:30</p></div>'
    assert detail_period(detail).start==dt("2026-11-12T15:30")


def test_criteria_boundaries():
    c=lecture(start=dt("2026-11-12 15:30"),end=dt("2026-11-12 17:30"))
    good=Criteria(keywords_any=["ai"],platforms=[c.platform],weekdays=[4],time_from="15:00",time_until="18:00",min_credits=.2)
    assert matches(c,good)
    assert not matches(c,Criteria(exclude=["AI"]))
    assert not matches(c,Criteria(time_until="17:00"))
    assert not matches(c,Criteria(busy=[Busy(start=c.start,end=c.end)]))
    assert matches(c,Criteria(busy=[Busy(start=c.end,end=c.end+timedelta(hours=1))]))
    assert not matches(c,Criteria(date_until=c.start))


@pytest.mark.parametrize("fields",[{"weekdays":[0]},{"time_from":"25:00"},{"time_from":"18:00","time_until":"09:00"}])
def test_invalid_criteria(fields):
    with pytest.raises(ValueError): Criteria(**fields)


def test_duplicate_and_cancel(tmp_path):
    s=store(tmp_path)
    e=Engine(s)
    c=lecture()
    j=e.enqueue(c,instruction="抢此课程")
    assert e.enqueue(c,instruction="抢此课程")["id"]==j["id"]
    j["state"]="auth_required";s.put("job",j["id"],j)
    assert e.enqueue(c,instruction="抢此课程")["id"]==j["id"]
    j["cancel_requested"]=True;s.put("job",j["id"],j)
    assert asyncio.run(e.step(j))["state"]=="cancelled"
    asyncio.run(e.client.close())


class Campus:
    def __init__(self, mode="success"):
        self.mode=mode
        self.posts=0
        self.enrolled=False
        self.calls=[]
        self.c=lecture()

    def __call__(self,req):
        self.calls.append((req.method,req.url.path))
        if req.method=="POST":
            self.posts+=1
            assert parse_qs(req.content.decode())=={"classid":["12345"],"SPAM":["form-token"]}
            if self.mode=="timeout_success":
                self.enrolled=True
                raise httpx.ReadTimeout("test",request=req)
            if self.mode=="timeout_unknown":
                raise httpx.ReadTimeout("test",request=req)
            if self.mode=="unknown":
                return httpx.Response(200,json={"unexpected":True})
            if self.mode=="full_after_post":
                return httpx.Response(200,json={"status":0,"info":"名额已满"})
            self.enrolled=True
            return httpx.Response(200,json={"status":1,"info":"报名成功"})
        if "apply/classid" in req.url.path:
            state="enrolled" if self.enrolled else ("not_open" if self.mode=="not_open" else "ready")
            return httpx.Response(200,text=form(state,quota=0 if self.mode=="full" else 2))
        if "/Class/index" in req.url.path:
            return httpx.Response(200,text=listing(self.c))
        return httpx.Response(200,text='<table><thead>课程名称 报名时间</thead></table>')


async def execute(tmp_path,mode):
    s=store(tmp_path)
    fake=Campus(mode)
    client=Client(s,transport=httpx.MockTransport(fake),session={"cookies":[]})
    e=Engine(s,client)
    j=e.enqueue(fake.c,instruction="按测试目标报名")
    await e.preflight(j)
    out=await e.step(j)
    await client.close()
    return out,fake,s


@pytest.mark.parametrize("mode,expected,posts",[("success","confirmed",1),("timeout_success","confirmed",1),
    ("timeout_unknown","verifying",1),("unknown","verifying",1),("full","full",0),
    ("not_open","waiting",0),("full_after_post","full",1)])
def test_enrollment_paths(tmp_path,mode,expected,posts):
    result,fake,s=asyncio.run(execute(tmp_path,mode))
    assert result["state"]==expected and fake.posts==posts
    assert "form-token" not in str(s.events())


def test_crash_recovery_never_resubmits(tmp_path):
    async def run():
        s=store(tmp_path); fake=Campus();fake.enrolled=True
        client=Client(s,transport=httpx.MockTransport(fake),session={"cookies":[]})
        e=Engine(s,client);j=e.enqueue(fake.c,instruction="抢此课程")
        e.save(j,"submitting")
        result=await e.step(j)
        await client.close()
        assert result["state"]=="confirmed" and fake.posts==0
    asyncio.run(run())


def test_dry_run_no_post(tmp_path):
    async def run():
        s=store(tmp_path);fake=Campus();client=Client(s,transport=httpx.MockTransport(fake),session={"cookies":[]})
        e=Engine(s,client);j=e.enqueue(fake.c,instruction="演练",dry_run=True)
        result=await e.step(j)
        await client.close()
        assert result["state"]=="dry_run" and fake.posts==0
    asyncio.run(run())


def test_rate_limit_honors_header(tmp_path):
    async def run():
        s=store(tmp_path);calls=[]
        s.defer_requests=lambda seconds:calls.append(seconds)
        client=Client(s,transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={"Retry-After":"123"})),session={"cookies":[]})
        with pytest.raises(RateLimited) as e: await client.apply("test_A")
        assert e.value.seconds==123 and calls==[123]
        await client.close()
    asyncio.run(run())


def test_pagination_complete_and_dedup(tmp_path):
    async def run():
        s=store(tmp_path)
        def handler(req):
            page2='page=2' in str(req.url)
            c=lecture(id="second" if page2 else "first")
            next_page='' if page2 else '<ul class="pagination"><a href="?navid=1&page=2">下一页</a></ul>'
            return httpx.Response(200,text=listing(c,next_page))
        client=Client(s,transport=httpx.MockTransport(handler),session={"cookies":[]})
        rows=await client.lectures()
        await client.close()
        assert {c.id for c in rows}=={"first","second"}
    asyncio.run(run())


def test_discovery_quota_and_monitor_mode(tmp_path):
    async def run():
        s=store(tmp_path);fake=Campus();client=Client(s,transport=httpx.MockTransport(fake),session={"cookies":[]})
        e=Engine(s,client)
        monitor=e.add_rule(Rule(name="看候选",instruction="找 AI 讲座",expires=now()+timedelta(days=1),mode="monitor"))
        await e.discover(monitor)
        assert not s.all("job") and s.all("candidates")
        item=e.add_rule(Rule(name="自动抢",instruction="抢一场 AI 讲座",expires=now()+timedelta(days=1),max_success=1))
        await e.discover(item);await e.discover(item)
        assert len(s.all("job"))==1 and fake.posts==0
        await client.close()
    asyncio.run(run())


def test_sqlite_worker_lease(tmp_path):
    s=Store(tmp_path/"test.db")
    assert s.lease("worker","a",10)
    assert not s.lease("worker","b",10)
    s.release("worker","a")
    assert s.lease("worker","b",10)
