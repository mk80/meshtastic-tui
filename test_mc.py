import asyncio
from meshcore import MeshCore

async def main():
    try:
        mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
        print("Connected!")
        print("Self Info:", mc.self_info)
        print("Contacts:", mc.contacts)
        await mc.disconnect()
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
