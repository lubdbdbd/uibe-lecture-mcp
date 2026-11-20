"""Optional timer-only loop for sandboxed Windows test runners without loopback.

MockTransport tests never use sockets. Production keeps the normal asyncio loop.
"""
import asyncio
import os
import selectors
import time

if os.environ.get("UIBE_TEST_NO_SOCKETS")=="1":
    class TimerSelector(selectors.SelectSelector):
        def select(self, timeout=None):
            if self.get_map():
                raise RuntimeError("离线测试不允许注册网络描述符")
            time.sleep(min(.01,timeout) if timeout is not None else .01)
            return []

    class OfflineLoop(asyncio.SelectorEventLoop):
        def __init__(self):
            super().__init__(TimerSelector())
        def _make_self_pipe(self): pass
        def _close_self_pipe(self): pass
        def _write_to_self(self): pass

    class OfflinePolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return OfflineLoop()

    asyncio.set_event_loop_policy(OfflinePolicy())
