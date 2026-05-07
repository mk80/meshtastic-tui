import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    fs = await mc.commands.get_default_flood_scope()
    print("Flood scope:", fs)
    await mc.disconnect()

asyncio.run(main())
