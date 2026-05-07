import asyncio
from meshcore import MeshCore, EventType

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("Connected!")
    
    async def on_event(event):
        print("EVENT:", event.type, event.attributes)
        
    mc.subscribe(EventType.ADVERTISEMENT, on_event)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_event)
    
    await asyncio.sleep(5)
    await mc.disconnect()

asyncio.run(main())
