from meshtastic.serial_interface import SerialInterface
try:
    interface = SerialInterface()
    lora = interface.localNode.localConfig.lora
    print("Meshtastic LoRa Config:")
    print("hop_limit:", lora.hop_limit)
    print("use_preset:", getattr(lora, 'use_preset', 'N/A'))
    # Print all attributes
    print(dir(lora))
    interface.close()
except Exception as e:
    print("Error:", e)
