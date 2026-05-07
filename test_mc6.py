import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("Testing get_channel...")
    try:
        # get_channel takes an index probably? Let's try 0 and 1
        ch0 = await mc.commands.get_channel(0)
        print("Channel 0:", ch0)
    except Exception as e:
        print("Error getting channel 0:", e)
        
    await mc.disconnect()

asyncio.run(main())
