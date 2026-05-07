import asyncio
from meshcore import MeshCore
import inspect

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("send_chan_msg signature:", inspect.signature(mc.commands.send_chan_msg))
    print("send_msg signature:", inspect.signature(mc.commands.send_msg))
    await mc.disconnect()

asyncio.run(main())
