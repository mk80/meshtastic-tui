import asyncio
from meshcore import MeshCore, EventType
import logging

logging.basicConfig(level=logging.DEBUG)

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM2', default_timeout=5)
    
    def on_msg(event):
        print(f"GOT MSG: type={event.type}, payload={event.payload}")

    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_msg)
    mc.subscribe(EventType.CONTACT_MSG_RECV, on_msg)
    
    print("Fetching messages...")
    while True:
        res = await mc.commands.get_msg(timeout=5.0)
        print(f"get_msg res: {res}")
        if not res or res.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
            break

    await mc.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
