import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("Fetching contacts...")
    try:
        contacts = await mc.commands.get_contacts()
        print("Contacts returned:", contacts)
    except Exception as e:
        print("get_contacts failed:", e)
    
    print("mc.contacts is now:", mc.contacts)
    print("Num contacts:", len(mc.contacts))
    await mc.disconnect()

asyncio.run(main())
