import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("Connected!")
    print(dir(mc))
    await mc.disconnect()

asyncio.run(main())
