import asyncio
import time
import httpx
import pytest
from uibe_lecture import auth, client as client_module, service
from uibe_lecture.client import Client
from uibe_lecture.storage import Store
from uibe_lecture.parser import AuthRequired, parse_lectures, parse_reservations
from uibe_lecture.engine import Engine


def cookie(value):
    return {"name":"PHPSESSID","value":value,"domain":"ea.uibe.edu.cn","path":"/","expires":-1,"secure":True,"httpOnly":True,"sameSite":"Lax"}


def setup_session(tmp_path,monkeypatch,value="old"):
    monkeypatch.setenv("UIBE_DATA_DIR",str(tmp_path))
    auth.save_session({"cookies":[cookie(value)],"origins":[]})
    store=Store(tmp_path/"state.sqlite3")
    store.reserve_slot=lambda _:0
    return store


@pytest.mark.parametrize("parse",[parse_lectures,parse_reservations])
def test_real_logged_out_interstitial(parse):
    with pytest.raises(AuthRequired):
        parse('<title>操作提示</title><div>请登录</div><p>3 秒后自动跳转</p>',"https://ea.uibe.edu.cn/User/Mine/index.html")


def test_encrypted_cookie_rotation_survives_new_client(tmp_path,monkeypatch):
    store=setup_session(tmp_path,monkeypatch)
    async def run():
        def rotate(request):
            return httpx.Response(200,text="<html>已登录</html>",headers={"Set-Cookie":"PHPSESSID=rotated; Path=/; Secure; HttpOnly"})
        first=Client(store,transport=httpx.MockTransport(rotate))
        await first.request("GET","/User/Mine/index.html")
        await first.close()
        saved=auth.load_session()
        assert saved["cookies"][0]["value"]=="rotated"
        assert b"rotated" not in auth.session_path().read_bytes()
        def next_process(request):
            assert request.headers["cookie"]=="PHPSESSID=rotated"
            return httpx.Response(200,text="ok")
        second=Client(store,transport=httpx.MockTransport(next_process))
        await second.request("GET","/User/Mine/index.html")
        await second.close()
    asyncio.run(run())


def test_stale_client_cannot_replace_new_login(tmp_path,monkeypatch):
    setup_session(tmp_path,monkeypatch)
    before=auth.load_session()
    auth.save_session({"cookies":[cookie("new-login")]})
    assert auth.save_session(dict(before,cookies=[cookie("stale")]),expected_saved_at=before["saved_at"]) is False
    assert auth.load_session()["cookies"][0]["value"]=="new-login"


def test_expired_get_recovers_once_and_retries_with_new_cookie(tmp_path,monkeypatch):
    store=setup_session(tmp_path,monkeypatch)
    calls=[]
    async def recover():
        calls.append("recovered")
        auth.save_session({"cookies":[cookie("renewed")]})
    monkeypatch.setattr(client_module,"recover_session",recover)
    def handle(req):
        calls.append(req.headers.get("cookie"))
        return httpx.Response(200,text="ok" if "renewed" in req.headers.get("cookie","") else "<title>操作提示</title>请登录")
    async def run():
        c=Client(store,transport=httpx.MockTransport(handle))
        assert (await c.request("GET","/User/Mine/index.html")).text=="ok"
        await c.close()
    asyncio.run(run())
    assert calls==["PHPSESSID=old","recovered","PHPSESSID=renewed"]


def test_post_is_never_replayed_after_auth_failure(tmp_path,monkeypatch):
    store=setup_session(tmp_path,monkeypatch)
    seen=[]
    async def recover():
        pytest.fail("POST must not trigger a replay")
    monkeypatch.setattr(client_module,"recover_session",recover)
    def handle(req):
        seen.append(req.method)
        return httpx.Response(200,text="<title>操作提示</title>请登录")
    async def run():
        c=Client(store,transport=httpx.MockTransport(handle))
        with pytest.raises(AuthRequired):
            await c.request("POST","/User/Mine/apply.html",data={})
        await c.close()
    asyncio.run(run())
    assert seen==["POST"]


def test_dead_login_child_does_not_report_open_window(tmp_path,monkeypatch):
    setup_session(tmp_path,monkeypatch)
    class Dead:
        pid=123
        returncode=1
        def poll(self): return 1
    monkeypatch.setattr(service.subprocess,"Popen",lambda *a,**kw:Dead())
    result=service.spawn_login()
    assert result["state"]=="failed"
    assert "未确认窗口" in result["message"]


def test_keepalive_is_readonly_and_throttled(tmp_path,monkeypatch):
    store=setup_session(tmp_path,monkeypatch)
    requests=[]
    def handle(req):
        requests.append(req.method)
        return httpx.Response(200,text="ok")
    async def run():
        c=Client(store,transport=httpx.MockTransport(handle))
        e=Engine(store,c)
        await e.keep_session_alive()
        await e.keep_session_alive()
        await c.close()
    asyncio.run(run())
    assert requests==["GET"]
    assert store.get("system","session_health")["logged_in"]
