import meshtastic
import meshtastic.serial_interface
import sys

def main():
    try:
        print("Attempting to connect to the Meshtastic radio...")
        # Without specifying a port, it automatically searches for one
        interface = meshtastic.serial_interface.SerialInterface()
        print("Successfully connected!")
        
        print("\n--- My Node Info ---")
        print(interface.myInfo)
        
        print("\n--- Nodes in Mesh ---")
        if interface.nodes:
            for node_id, node_info in interface.nodes.items():
                print(f"Node ID: {node_id}")
                print(f"User: {node_info.get('user', {})}")
                print(f"Position: {node_info.get('position', {})}")
                print("-" * 20)
        else:
            print("No other nodes found.")

        interface.close()
        
    except Exception as e:
        print(f"Error connecting to radio: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
