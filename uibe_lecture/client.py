from __future__ import annotations

import asyncio
import time
from email.utils import parsedate_to_datetime
import httpx
from .auth import load_session, session_path, save_session, recover_session
from .models import ORIGIN, class_id, dt, now
from .parser import AuthRequired, RateLimited, SiteError, FormExpired, parse_apply, parse_lectures, parse_reservations, soup_of, detail_period, site_url
from .pacing import RequestGate


class Client:
    def __init__(self, store, transport=None, session=None, *, transport_factory=None):
        self.store=store
        self.external_session=session
        self.version=0
        self.session=session
        self.gate=RequestGate(store)
        self.recovery_lock=asyncio.Lock()
        self.catalog_lock=asyncio.Lock()
        self.catalog_cache=None
        if transport is not None and transport_factory is not None:
            raise ValueError("Specify transport or transport_factory, not both")
        self._transport_factory=transport_factory
        # A caller-owned transport may not be reusable once closed.
        # Such callers can supply a factory to opt into safe pool replacement.
        self._can_rotate=transport is None
        self._active=0
        self._idle=asyncio.Event()
        self._idle.set()
        self._rotation=None
        self._pool_timeouts=0
        self._closing=False
        self.pool_rotations=0
        self.http=self._new_http(transport)

    def _new_http(self, transport=None):
        if self._transport_factory is not None:
            transport=self._transport_factory()
        return httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=httpx.Timeout(12,connect=5),
                                    limits=httpx.Limits(max_connections=8,max_keepalive_connections=8,keepalive_expiry=90),
                                    headers={"Cache-Control":"no-cache"})

    async def close(self):
        self._closing=True
        await self._idle.wait()
        if self._rotation is not None:
            await asyncio.shield(self._rotation)
        await self.http.aclose()

    async def _rotate_pool(self):
        # New leases wait for this task. Existing requests finish parsing and
        # persisting cookies before their pool is closed. No request is replayed.
        await self._idle.wait()
        old=self.http
        fresh=self._new_http()
        fresh.timeout=old.timeout
        fresh.headers.update(old.headers)
        fresh.cookies.update(old.cookies)
        try:
            await old.aclose()
        except Exception:
            await fresh.aclose()
            raise
        self.http=fresh
        self._pool_timeouts=0
        self.pool_rotations+=1

    def sync_session(self):
        version=1 if self.external_session is not None else (session_path().stat().st_mtime_ns if session_path().exists() else 0)
        if version != self.version or not self.session:
            self.session=self.external_session if self.external_session is not None else load_session()
            self.version=version
            self.http.cookies.clear()
            for c in self.session.get("cookies",[]):
                if c.get("expires",-1)>=0 and c["expires"]<=time.time():
                    continue
                if c["domain"].lstrip(".") in ("ea.uibe.edu.cn","uibe.edu.cn"):
                    self.http.cookies.set(c["name"],c["value"],domain=c["domain"],path=c.get("path","/"))
            self.http.headers["User-Agent"]=self.session.get("user_agent","UIBE-Lecture/0.1")

    def persist_cookies(self):
        if self.external_session is not None:
            return
        old={(c["domain"],c.get("path","/"),c["name"]):c for c in self.session.get("cookies",[])}
        cookies=[c for c in old.values() if c["domain"].lstrip(".") not in ("ea.uibe.edu.cn","uibe.edu.cn")]
        for c in self.http.cookies.jar:
            if c.domain.lstrip(".") not in ("ea.uibe.edu.cn","uibe.edu.cn"):
                continue
            item=dict(old.get((c.domain,c.path,c.name),{}))
            item.update(name=c.name,value=c.value,domain=c.domain,path=c.path)
            # Existing session cookies retain their browser metadata. New Set-Cookie
            # metadata comes from the response cookie jar.
            if c.expires is not None or not item.get("expires"):
                item["expires"]=c.expires if c.expires is not None else -1
            if c.secure or "secure" not in item:
                item["secure"]=c.secure
            item.setdefault("httpOnly",False)
            item.setdefault("sameSite","Lax")
            cookies.append(item)
        key=lambda c:(c["domain"],c.get("path","/"),c["name"])
        if sorted(cookies,key=key)!=sorted(self.session.get("cookies",[]),key=key):
            save_session(dict(self.session,cookies=cookies),expected_saved_at=self.session.get("saved_at"))

    async def request(self, method, path, **kwargs):
        try:
            return await self._request_once(method,path,**kwargs)
        except AuthRequired:
            # Never replay a POST: its server-side result could be uncertain.
            if method!="GET" or self.external_session is not None:
                raise
            failed_version=self.version
            async with self.recovery_lock:
                version=session_path().stat().st_mtime_ns if session_path().exists() else 0
                if version==failed_version:
                    try:
                        await recover_session()
                    except Exception:
                        raise AuthRequired("学校会话已过期，自动恢复未成功；请完成一次专用窗口登录，后续将自动复用") from None
                self.version=0
            return await self._request_once(method,path,**kwargs)

    async def _request_once(self, method, path, **kwargs):
        if self._closing:
            raise RuntimeError("Client is closing")
        if self._rotation is not None:
            rotation=self._rotation
            await asyncio.shield(rotation)
            if self._rotation is rotation:
                self._rotation=None
        if self._closing:
            raise RuntimeError("Client is closing")
        self._active+=1
        self._idle.clear()
        try:
            result=await self._request_in_pool(method,path,**kwargs)
            self._pool_timeouts=0
            return result
        except httpx.PoolTimeout:
            self._pool_timeouts+=1
            if self._can_rotate and self._pool_timeouts>=3 and self._rotation is None:
                self._rotation=asyncio.create_task(self._rotate_pool())
            raise
        except Exception:
            self._pool_timeouts=0
            raise
        finally:
            self._active-=1
            if not self._active:
                self._idle.set()

    async def _request_in_pool(self, method, path, *, data=None, fast=False, referer=None, before_send=None, expected_version=None):
        url=site_url(path)
        if method=="POST" and url != ORIGIN+"/User/Mine/apply.html":
            raise SiteError("不支持的写入地址")
        interval=0.05 if method=="POST" else 0.25 if fast else 0.5
        await self.gate.acquire(interval,critical=method=="POST",urgent=fast)
        self.sync_session()
        if expected_version is not None and expected_version!=self.version:
            raise FormExpired("登录会话已更新，重新读取报名表")
        headers={"Referer":referer or ORIGIN+"/User/Class/index.html"}
        if method=="POST":
            headers.update({"X-Requested-With":"XMLHttpRequest","Origin":ORIGIN,"Accept":"application/json, text/javascript, */*; q=0.01"})
        if before_send:
            before_send()
        sent_at=time.time()
        sent_monotonic=time.monotonic()
        timeout=httpx.Timeout(1.5,connect=1,pool=0.5,write=1.5) if fast and method=="GET" else self.http.timeout
        r=await self.http.request(method,url,data=data,headers=headers,timeout=timeout)
        received_at=time.time()
        r.extensions["uibe_sent_at"]=sent_at
        r.extensions["uibe_sent_monotonic"]=sent_monotonic
        r.extensions["uibe_response_ms"]=(received_at-sent_at)*1000
        # HTTPS Date provides the school's clock; reject slow/cached samples.
        if method=="GET" and r.status_code==200 and received_at-sent_at<2 and r.headers.get("Date") and not r.headers.get("Age"):
            try:
                offset=parsedate_to_datetime(r.headers["Date"]).timestamp()+0.5-(sent_at+received_at)/2
                if abs(offset)<300:
                    previous=self.store.get("system","school_clock") or {}
                    bounds=previous.get("bounds",[]) if received_at-previous.get("checked_at",0)<120 else []
                    stamp=parsedate_to_datetime(r.headers["Date"]).timestamp()
                    bounds=(bounds+[[stamp-received_at,stamp+1-sent_at]])[-30:]
                    lower=max(b[0] for b in bounds)
                    upper=min(b[1] for b in bounds)
                    if lower>upper:
                        # Different server nodes or a clock change: rebuild the estimate.
                        bounds=bounds[-1:]
                        lower,upper=bounds[0]
                    self.store.put("system","school_clock",{"checked_at":received_at,"offset_seconds":(lower+upper)/2,
                                   "uncertainty_seconds":(upper-lower)/2,"bounds":bounds})
            except (ValueError,TypeError,OverflowError):
                pass
        if r.status_code==429:
            v=r.headers.get("Retry-After","60")
            try:
                seconds=float(v)
            except ValueError:
                try:
                    reference=parsedate_to_datetime(r.headers["Date"]).timestamp() if r.headers.get("Date") else time.time()
                    seconds=max(1,parsedate_to_datetime(v).timestamp()-reference)
                except Exception:
                    seconds=60
            self.store.defer_requests(seconds)
            raise RateLimited(seconds)
        if 300<=r.status_code<400 or r.status_code in (401,403):
            raise AuthRequired("学校登录已失效或访问被拒，请检查登录状态")
        r.raise_for_status()
        if any(x in r.text for x in ("请求过于频繁","操作太频繁","访问过于频繁")):
            self.store.defer_requests(60)
            raise RateLimited(60)
        r.extensions["uibe_soup"]=soup_of(r.text)
        self.persist_cookies()
        return r

    async def paged(self, path, parser, max_pages=100):
        queue=[site_url(path)]
        visited=set()
        rows=[]
        while queue:
            url=queue.pop(0)
            if url in visited:
                continue
            if len(visited)>=max_pages:
                raise SiteError("分页超过上限，未完成列表读取")
            visited.add(url)
            r=await self.request("GET",url)
            items,links=parser(r.text,url)
            rows.extend(items)
            queue.extend(x for x in links if x not in visited)
        return rows

    async def lectures(self):
        async with self.catalog_lock:
            if self.catalog_cache and time.monotonic()-self.catalog_cache[0]<2:
                return self.catalog_cache[1]
            result=await self._lectures()
            self.catalog_cache=(time.monotonic(),result)
            return result

    async def _lectures(self):
        found={}
        for nav in (1,2):
            for c in await self.paged(f"/User/Class/index.html?navid={nav}",parse_lectures):
                found[c.id]=c
        for c in found.values():
            self.store.put("lecture",c.id,c.model_dump(mode="json"))
        return sorted(found.values(),key=lambda x:(x.opens,x.start,x.id))

    async def apply(self, cid, fast=False):
        path=f"/User/Mine/apply/classid/{class_id(cid)}.html"
        r=await self.request("GET",path,fast=fast)
        form=parse_apply(r.text,cid,soup=r.extensions.get("uibe_soup"))
        form.session_version=self.version
        form.received_monotonic=time.monotonic()
        return form

    async def submit(self, cid, form, before_send=None):
        if form.state!="ready" or form.course_id!=class_id(cid):
            raise SiteError("提交前状态不符合报名条件")
        r=await self.request("POST",form.action,data=form.fields,fast=True,
                             referer=ORIGIN+f"/User/Mine/apply/classid/{cid}.html",before_send=before_send,
                             expected_version=form.session_version)
        try:
            value=r.json()
        except ValueError:
            soup_of(r.text)
            raise SiteError("提交返回格式变化，等待核实报名状态") from None
        if not isinstance(value,dict):
            raise SiteError("提交返回格式变化")
        # Keep no token-bearing response payloads.
        return {"status":value.get("status"),"message":str(value.get("msg",value.get("info","")))[:300],
                "sent_at":r.extensions["uibe_sent_at"],"response_ms":round(r.extensions["uibe_response_ms"],2),
                "ready_to_send_ms":round((r.extensions["uibe_sent_monotonic"]-form.received_monotonic)*1000,2) if form.received_monotonic else None}

    async def reservations(self, periods=False):
        rows=await self.paged("/User/Mine/index.html",parse_reservations)
        if periods:
            for row in rows:
                cached=self.store.get("period",row["detail_url"])
                if not cached or (dt(cached["end"])>now() and time.time()-cached.get("fetched_at",0)>300):
                    r=await self.request("GET",row["detail_url"])
                    period=detail_period(r.text)
                    cached=period.model_dump(mode="json")
                    cached["fetched_at"]=time.time()
                    self.store.put("period",row["detail_url"],cached)
                row["period"]=dict(cached,title=row["title"])
        return rows

    async def details(self, lecture):
        r=await self.request("GET",lecture.detail_url)
        soup=soup_of(r.text)
        for e in soup.select("script,style,nav,.side-menu"):
            e.decompose()
        return {"lecture":lecture.model_dump(mode="json"),"text":soup.get_text(" ",strip=True)[-16000:]}

