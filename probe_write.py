import asyncio, os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','workflow_backend.settings.local')
os.environ['AGENT_CHECKPOINT_PATH'] = r'C:\Users\91700\AppData\Local\Temp\ckpt_probe.sqlite3'
django.setup()
from langchain_core.messages import HumanMessage
async def main():
    from chat.turn.agent import get_graph
    from chat.turn import checkpoints
    g = get_graph()
    await checkpoints.setup(g.checkpointer)
    cfg = {"configurable": {"thread_id": "probe-thread"}}
    await g.checkpointer.aput(cfg, {"v": 4, "id": "c1", "ts": "2026-09-04T00:00:00+00:00",
        "channel_values": {"messages": [HumanMessage(content="survive me")]},
        "channel_versions": {}, "versions_seen": {}}, {"source": "input", "step": 0}, {})
    print("WROTE")
asyncio.run(main())
