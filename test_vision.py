import base64
import json
import urllib.request

with open("./sprite_test.png", "rb") as f:
    img = base64.b64encode(f.read()).decode()

payload = {
    "model": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL",
    "messages": [
        {
            "role": "user",
            "content": "Describe this image in one sentence.",
            "images": [img],
        }
    ],
    "stream": False,
}

req = urllib.request.Request(
    "http://localhost:11434/api/chat",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(req, timeout=180) as r:
    resp = json.loads(r.read().decode())

print(resp["message"]["content"])
