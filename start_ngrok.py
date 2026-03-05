"""Start ngrok using NGROK_AUTHTOKEN and NGROK_URL from .env.

Tunnels to WEBHOOK_PORT so BTCPay can reach the bot's webhook.
After starting, set WEBHOOK_PUBLIC_BASE_URL to the ngrok URL (e.g. https://your-subdomain.ngrok.app).
"""

import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

token = (os.getenv("NGROK_AUTHTOKEN") or "").strip()
url = (os.getenv("NGROK_URL") or "").strip()
port = (os.getenv("WEBHOOK_PORT") or os.getenv("NGROK_PORT") or "8080").strip()

if not token or token == "paste_your_authtoken_here":
    print(
        "ERROR: Set NGROK_AUTHTOKEN in .env "
        "(get it from https://dashboard.ngrok.com/get-started/your-authtoken)"
    )
    sys.exit(1)

ngrok_exe = os.getenv("NGROK_PATH", r"C:\Program Files\ngrok\ngrok.exe")
cmd = [ngrok_exe, "http", port]
if url:
    cmd.insert(2, f"--url={url}")
env = os.environ.copy()
env["NGROK_AUTHTOKEN"] = token

print(f"Starting ngrok → localhost:{port}")
if url:
    print(f"Reserved domain: https://{url}")
    print(f"→ Set WEBHOOK_PUBLIC_BASE_URL=https://{url} in .env")
    print(f"  BTCPay webhook: https://{url}/webhooks/btcpay")
subprocess.run(cmd, env=env)
