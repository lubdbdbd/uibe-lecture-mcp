"""Isolated loopback tests exercising the real httpcore connection pool."""
import asyncio
import httpx
import pytest

from uibe_lecture.client import Client
from uibe_lecture.engine import Engine
from test_core import Campus, store


class LocalTransport(httpx.AsyncHTTPTransport):
    def __init__(self, port):
        super().__init__(limits=httpx.Limits(max_connections=1), retries=0)
        self.port=port

    async def handle_async_request(self, request):
        request.url=request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return await super().handle_async_request(request)


async def no_gate(*args, **kwargs):
    pass


def test_real_pool_saturation_rotation_waits_for_inflight_and_preserves_cookies(tmp_path):
    async def run():
        entered=asyncio.Event()
        release=asyncio.Event()
        calls=[]
        async def handler(reader, writer):
            try:
                head=await reader.readuntil(b"\r\n\r\n")
                calls.append(head)
                if len(calls)==1:
                    entered.set()
                    await release.wait()
                body=b"<html>ok</html>"
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(handler,"127.0.0.1",0)
        port=server.sockets[0].getsockname()[1]
        c=Client(store(tmp_path), session={"cookies":[]}, transport_factory=lambda:LocalTransport(port))
        c.gate.acquire=no_gate
        c.sync_session()
        c.http.cookies.set("test_session","synthetic",domain="ea.uibe.edu.cn",path="/")
        old=c.http
        held=asyncio.create_task(c.request("GET","/held"))
        await entered.wait()
        c.http.timeout=httpx.Timeout(.1)
        submissions=[]
        results=await asyncio.gather(
            c.request("GET","/probe",fast=True),
            c.request("GET","/probe",fast=True),
            c.request("POST","/User/Mine/apply.html", data={"synthetic":"test"},
                      before_send=lambda:submissions.append("attempt")),
            return_exceptions=True)
        assert submissions==["attempt"]
        assert all(isinstance(r,httpx.PoolTimeout) for r in results)
        assert c.pool_rotations==0 and not old.is_closed
        waiting=asyncio.create_task(c.request("GET","/after"))
        await asyncio.sleep(.02)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert c._rotation is not None and not c._rotation.cancelled()
        waiting=asyncio.create_task(c.request("GET","/after"))
        release.set()
        await held
        await waiting
        assert c.pool_rotations==1 and old.is_closed
        assert len(calls)==2
        assert b"test_session=synthetic" in calls[1]
        await c.close()
        server.close()
        await server.wait_closed()
    asyncio.run(run())


def test_real_post_disconnect_is_verified_without_replay(tmp_path):
    async def run():
        campus=Campus()
        async def handler(reader,writer):
            try:
                head=await reader.readuntil(b"\r\n\r\n")
                lines=head.decode().split("\r\n")
                method,path,_=lines[0].split()
                headers=dict(line.split(": ",1) for line in lines[1:] if ": " in line)
                body=await reader.readexactly(int(headers.get("Content-Length","0")))
                if method=="POST":
                    campus.posts+=1
                    # Submission reached server; outcome intentionally unavailable.
                    return
                response=campus(httpx.Request(method,"https://ea.uibe.edu.cn"+path,content=body))
                data=response.content
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "+str(len(data)).encode()+b"\r\n\r\n"+data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(handler,"127.0.0.1",0)
        port=server.sockets[0].getsockname()[1]
        c=Client(store(tmp_path),session={"cookies":[]},transport_factory=lambda:LocalTransport(port))
        c.gate.acquire=no_gate
        e=Engine(c.store,c)
        j=e.enqueue(campus.c,instruction="local isolated test")
        j["preflight_at"]=e.clock()
        e.save(j)
        result=await e.step(j)
        assert result["state"]=="verifying" and campus.posts==1
        campus.enrolled=True
        result=await e.step(result)
        assert result["state"]=="confirmed" and campus.posts==1
        await c.close()
        server.close()
        await server.wait_closed()
    asyncio.run(run())
