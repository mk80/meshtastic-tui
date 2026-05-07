import requests
import json

payload = {
    "message": "test channel message from script",
    "channel": 0
}
try:
    res = requests.post("http://localhost:5000/api/send", json=payload)
    print("Sent:", res.status_code, res.text)
except Exception as e:
    print("Error:", e)
