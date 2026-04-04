import sys
import requests

def main():
    if len(sys.argv) < 2:
        print("Usage: python send.py 'Your message' [destination_node_id]")
        print("Example (broadcast): python send.py 'Hello mesh'")
        print("Example (direct msg): python send.py 'Hello node!' '!12345678'")
        sys.exit(1)

    message = sys.argv[1]
    
    payload = {'message': message}
    if len(sys.argv) >= 3:
        payload['destination'] = sys.argv[2]
        
    try:
        print("Sending request to local Meshtastic daemon...")
        response = requests.post("http://localhost:5000/api/send", json=payload)
        
        if response.status_code == 200:
            print("Message sent/queued successfully!")
        else:
            print(f"Error from daemon: {response.text}")
            
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to the local daemon. Is app.py running?")

if __name__ == "__main__":
    main()
