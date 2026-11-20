from __future__ import annotations

import asyncio
import itertools


class RequestGate:
    """FIFO reads, preemptible by a single ready-form submission per course."""

    def __init__(self, store):
        self.store=store
        self.sequence=itertools.count()
        self.waiters={}

    def wake(self):
        for event in self.waiters.values():
            event.set()

    async def acquire(self, interval, critical=False, urgent=False):
        ticket=(0 if critical else 1 if urgent else 2,next(self.sequence))
        event=asyncio.Event()
        self.waiters[ticket]=event
        self.wake()
        try:
            while True:
                event.clear()
                delay=60.0
                if ticket==min(self.waiters):
                    delay=self.store.claim_request_slot(interval,critical)
                    if delay<=0:
                        return
                try:
                    await asyncio.wait_for(event.wait(),delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.waiters.pop(ticket,None)
            self.wake()
