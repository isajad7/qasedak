"""Read credential-free panel origins from SSH stdin; print safe probe results only."""
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

for line in sys.stdin:
    try:
        item = json.loads(line)
        if not isinstance(item, dict) or "origin" not in item:
            continue
        started = time.monotonic()
        result = {"panel_id": item["panel_id"], "location": "github_runner", "action": "unauthenticated_api"}
        try:
            with urlopen(Request(urljoin(item["origin"].rstrip("/") + "/", "api/system"), headers={"User-Agent": "qasedak-connectivity-check"}), timeout=30) as response:
                result["http_status"] = response.status
        except HTTPError as exc:
            result["http_status"] = exc.code
            exc.close()
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            reason = getattr(exc, "reason", None)
            if isinstance(reason, BaseException):
                result["reason_type"] = type(reason).__name__
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"probe_input_error_type": type(exc).__name__}), flush=True)
