import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=2)
    print("Self Info:")
    for k, v in mc.self_info.items():
        print(f"  {k}: {v}")
    
    print("\nChannels in MeshCore instance:")
    channels = getattr(mc, 'channels', None)
    print(channels)
    
    # Try looking in commands.messaging
    # or commands.device
    
    await mc.disconnect()

asyncio.run(main())
