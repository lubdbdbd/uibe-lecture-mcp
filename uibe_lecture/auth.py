from __future__ import annotations

import asyncio
import ctypes
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from .models import ORIGIN
from .storage import data_dir
from .parser import AuthRequired


def _dpapi(data: bytes, decrypt=False) -> bytes:
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_ubyte))]
    buf=(ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src=Blob(len(data),buf)
    dest=Blob()
    crypt=ctypes.windll.crypt32
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dest)):
        raise OSError("Windows 登录状态加密失败")
    try:
        return ctypes.string_at(dest.pbData,dest.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.pbData)


def session_path():
    return data_dir()/"session.bin"


@contextmanager
def file_lock(path, *, blocking=False):
    handle=path.open("a+b")
    try:
        handle.seek(0)
        if os.name=="nt":
            import msvcrt
            if path.stat().st_size==0:
                handle.write(b"0"); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX if blocking else fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def save_session(value, expected_saved_at=None):
    # Serialize independent MCP/worker writers; never replace a newer login.
    with file_lock(data_dir()/"session.lock",blocking=True):
        if expected_saved_at is not None:
            try:
                if load_session().get("saved_at") != expected_saved_at:
                    return False
            except AuthRequired:
                return False
        value=dict(value,saved_at=time.time())
        data=json.dumps(value,ensure_ascii=False).encode()
        payload=b"DP1"+_dpapi(data) if os.name=="nt" else b"JS1"+data
        p=session_path()
        tmp=p.with_name(p.name+"."+uuid.uuid4().hex+".tmp")
        try:
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"wb") as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def load_session():
    try:
        data=session_path().read_bytes()
    except FileNotFoundError:
        raise AuthRequired("首次使用请运行 auth login，在打开的学校页面登录一次") from None
    if data[:3]==b"DP1":
        data=_dpapi(data[3:],True)
    elif data[:3]==b"JS1":
        data=data[3:]
    else:
        raise AuthRequired("登录状态文件无法识别，请重新登录")
    return json.loads(data)


def set_login_status(state, **fields):
    # Public status only. Never write cookies, passwords, or token-bearing URLs.
    from .storage import Store
    Store().put("system","login",dict(state=state,pid=os.getpid(),at=time.time(),**fields))


def campus_domain(domain):
    return domain.lstrip(".")=="uibe.edu.cn" or domain.lstrip(".").endswith(".uibe.edu.cn")


async def launch_context(playwright, headless):
    profile=data_dir()/"browser-profile"
    channels=[{"channel":"chrome"},{"channel":"msedge"},{}]
    explicit=os.environ.get("UIBE_BROWSER_EXECUTABLE")
    if explicit:
        channels.insert(0,{"executable_path":explicit})
    for options in channels:
        try:
            return await playwright.chromium.launch_persistent_context(
                str(profile),headless=headless,timeout=20000,**options)
        except Exception:
            continue
    raise AuthRequired("学校登录浏览器启动失败；Chrome、Edge及内置浏览器均未能启动")


async def capture(context, page):
    state=await context.storage_state()
    cookies=[c for c in state["cookies"] if campus_domain(c["domain"])]
    origins=[o for o in state.get("origins",[]) if campus_domain(urlsplit(o["origin"]).hostname or "")]
    save_session({"cookies":cookies,"origins":origins,
                  "user_agent":await page.evaluate("navigator.userAgent")})


CAS_LOGIN_URL = "https://cas.uibe.edu.cn/cas/login?service=https%3A%2F%2Fea.uibe.edu.cn%2FUser%2FLogin%2Fcas.html"
LOGOUT_SELECTOR = 'a[href="/User/Login/logout.html"]'


async def prepare_login_page(page):
    """Bound the school's delayed login redirect before trying saved credentials."""
    parsed = urlsplit(page.url)
    if (parsed.scheme == "https" and parsed.netloc == "ea.uibe.edu.cn"
            and parsed.path == "/User/Class/index.html"):
        # A logged-out index can render a message before its JS redirect. Give
        # ordinary session/SSO restoration a chance without waiting indefinitely.
        try:
            await page.wait_for_function("""() =>
                location.pathname !== '/User/Class/index.html' ||
                !!document.querySelector('a[href="/User/Login/logout.html"]')
            """, timeout=5000)
        except Exception:
            pass
        parsed = urlsplit(page.url)
    if (parsed.scheme == "https" and parsed.netloc == "ea.uibe.edu.cn"
            and parsed.path in {"/User/Class/index.html", "/User/Login/index.html"}):
        if await page.locator(LOGOUT_SELECTOR).count():
            return
        # Navigate only to the pinned CAS service, never a link from page text.
        await page.goto(CAS_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    elif (parsed.scheme == "http" and parsed.netloc == "cas.uibe.edu.cn"
          and parsed.path == "/cas/login"):
        await page.goto(CAS_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)


async def login(timeout_seconds=600, *, interactive=True):
    from playwright.async_api import async_playwright
    profile=data_dir()/"browser-profile"
    if not interactive and not (profile/"Default").exists():
        raise AuthRequired("已保存的学校会话过期，需要在专用登录窗口完成一次登录")
    try:
        with file_lock(data_dir()/"browser.lock"):
            if interactive:
                set_login_status("starting")
            async with async_playwright() as p:
                context=await launch_context(p,not interactive)
                try:
                    page=context.pages[0] if context.pages else await context.new_page()
                    # Chromium discards session cookies on exit. Always seed the
                    # encrypted snapshot, including cookies refreshed by HTTP clients.
                    try:
                        old=load_session()
                        valid=[c for c in old.get("cookies",[]) if c.get("expires",-1)<0 or c["expires"]>time.time()]
                        if valid:
                            await context.add_cookies(valid)
                        origins={o["origin"]:o.get("localStorage",[]) for o in old.get("origins",[])}
                        if origins:
                            await context.add_init_script("const saved="+json.dumps(origins)+"; for (const item of saved[location.origin] || []) { if (localStorage.getItem(item.name) === null) localStorage.setItem(item.name,item.value); }")
                    except AuthRequired:
                        pass
                    if interactive:
                        # Report readiness only after an actual browser page exists.
                        await page.bring_to_front()
                        set_login_status("window_open")
                    await page.goto(ORIGIN+"/User/Class/index.html",wait_until="domcontentloaded",timeout=60000)
                    # Recover the ordinary saved session first, then use the opt-in
                    # credential store only on the verified HTTPS CAS form.
                    from .autologin import try_saved_login
                    await prepare_login_page(page)
                    auto_state=await try_saved_login(page)
                    if auto_state == "challenge_required" and not interactive:
                        raise AuthRequired("School login challenge requires user interaction")
                    limit=time.monotonic()+timeout_seconds
                    while time.monotonic()<limit:
                        if page.is_closed():
                            raise AuthRequired("登录窗口已关闭，尚未保存新的登录状态")
                        if page.url.startswith(ORIGIN+"/") and await page.locator('a[href="/User/Login/logout.html"]').count():
                            await capture(context,page)
                            from .storage import Store
                            Store().put("system","autologin",{"state":"success","attempt_at":time.time()})
                            if interactive:
                                set_login_status("saved")
                            return {"ok":True,"message":"登录已持久保存，后续自动复用并刷新会话"}
                        await asyncio.sleep(0.5)
                    raise AuthRequired("学校会话无法自动恢复，需要完成登录" if not interactive else "等待学校登录超时")
                finally:
                    await context.close()
    except Exception as exc:
        if interactive:
            # Exception type is enough for diagnostics; browser traces may contain tokens.
            set_login_status("failed",error=type(exc).__name__,message="登录窗口启动或登录未完成")
        raise


async def recover_session():
    # Silent recovery only; never surprise the user with an interactive window.
    return await login(timeout_seconds=15,interactive=False)
