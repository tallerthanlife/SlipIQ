import requests
import os
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GROQ_API_KEY")
print(f"Key found: {key[:10]}..." if key else "NO KEY FOUND")

r = requests.post(
    "https://api.groq.com/openai/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    },
    json={
        "model": "llama3-70b-8192",
        "messages": [{"role": "user", "content": "say hi"}],
        "max_tokens": 10
    }
)

print(f"Status: {r.status_code}")
print(f"Response: {r.text}")