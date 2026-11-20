import asyncio
from uibe_lecture.engine import Engine
from uibe_lecture.storage import Store


def test_worker_singleton_and_heartbeat(tmp_path):
    async def run():
        s=Store(tmp_path/"state.sqlite3")
        e=Engine(s)
        async def idle():
            await asyncio.sleep(.01)
        e.tick=idle
        task=asyncio.create_task(e.run())
        await asyncio.sleep(.05)
        assert s.get("system","worker")["heartbeat"]>0
        other=Engine(s)
        await asyncio.wait_for(other.run(),.5)
        assert not task.done()
        await other.client.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
