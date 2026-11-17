"""Send a signal to the local bridge exactly as TradingView would.

    .venv\\Scripts\\python.exe scripts\\send_test_signal.py buy
    .venv\\Scripts\\python.exe scripts\\send_test_signal.py sell
    .venv\\Scripts\\python.exe scripts\\send_test_signal.py close

Unless DRY_RUN=true in .env, this places a real order on the logged-in MT5 account.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
action = sys.argv[1] if len(sys.argv) > 1 else "buy"
payload = {
    "secret": os.environ["WEBHOOK_SECRET"],
    "action": action,
    "symbol": os.getenv("DEFAULT_SYMBOL", "XAUUSD"),
    "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
req = urllib.request.Request(f"http://127.0.0.1:{os.getenv('PORT', '80')}/webhook",
                             data=json.dumps(payload).encode(), headers={"Content-Type": "text/plain"})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.status, resp.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
