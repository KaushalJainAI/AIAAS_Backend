import asyncio, os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','workflow_backend.settings.local')
os.environ['AGENT_CHECKPOINT_PATH'] = r'C:\Users\91700\AppData\Local\Temp\ckpt_probe.sqlite3'
django.setup()
async def main():
    from chat.turn.agent import get_graph
    g = get_graph()
    cfg = {"configurable": {"thread_id": "probe-thread"}}
    tup = await g.checkpointer.aget_tuple(cfg)
    msgs = tup.checkpoint["channel_values"]["messages"] if tup else None
    print("READ BACK IN A NEW PROCESS:", [m.content for m in msgs] if msgs else None)
asyncio.run(main())
