#!/usr/bin/env python3
"""
Scrape the REAL Ollama Cloud usage % off ollama.com/settings.

Ollama Cloud publishes no usage API — the session (5h) + weekly utilization
is only shown on the logged-in settings page. This scraper connects to the
running Chrome instance via CDP (Chrome DevTools Protocol) on port 18800,
which is the same Chrome that clawdbot/Claude Code uses (already logged in).

Uses raw socket WS handshake (no Origin header) to bypass Chrome's
--remote-allow-origins restriction.

Writes results to ~/.local/share/orchestratormaxxing/ollama-usage.json.

Exit codes: 0 = scraped OK · 2 = not logged in · 3 = parse error · 4 = no Chrome
"""
import json
import os
import re
import socket
import sys
import time
import struct
from pathlib import Path

STORE = Path.home() / ".local" / "share" / "orchestratormaxxing" / "ollama-usage.json"
SETTINGS_URL = "https://ollama.com/settings"
CDP_PORT = 18800

_SESSION_WORDS = re.compile(r"(session|5[\s-]?hour|five[\s-]?hour|hourly)", re.I)
_WEEKLY_WORDS = re.compile(r"(week|7[\s-]?day|seven[\s-]?day)", re.I)
_TIER_RE = re.compile(r"\b(free|pro|max)\b", re.I)


def _write(payload: dict) -> None:
    payload = dict(payload)
    if payload.get("ok") is False:
        try:
            previous = json.loads(STORE.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            previous = {}
        last_good = previous if previous.get("ok") else previous.get("last_good")
        if isinstance(last_good, dict) and last_good.get("ok"):
            payload["last_good"] = last_good

    STORE.parent.mkdir(parents=True, exist_ok=True)
    temp = STORE.with_name(f".{STORE.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, indent=2))
    os.replace(temp, STORE)


def _http_get(path: str) -> str:
    """Simple HTTP GET to CDP endpoint via urllib."""
    import urllib.request
    url = f"http://127.0.0.1:{CDP_PORT}{path}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def _ws_connect(ws_url: str) -> socket.socket:
    """Connect to a CDP WebSocket endpoint using raw sockets (no Origin header)."""
    from urllib.parse import urlparse
    parsed = urlparse(ws_url)
    host = "127.0.0.1"  # Chrome binds to localhost, force IPv4
    port = parsed.port or CDP_PORT
    path = parsed.path

    # RFC 6455 §1.3 sample nonce — a spec constant for the handshake, not a secret.
    ws_nonce = "dGhlIHNhbXBsZSBub25jZQ=="
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_nonce}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock = socket.create_connection((host, port), timeout=15)
    sock.sendall(handshake.encode())
    resp = sock.recv(4096)
    if b"101" not in resp[:50]:
        raise ConnectionError(f"WS handshake failed: {resp[:100]}")
    return sock


def _ws_send(sock: socket.socket, msg: dict) -> None:
    """Send a JSON message as a masked WS text frame."""
    payload = json.dumps(msg).encode()
    mask_key = os.urandom(4)
    # Frame header: FIN=1, opcode=1 (text), mask=1
    if len(payload) < 126:
        header = struct.pack(">BB", 0x81, 0x80 | len(payload))
    elif len(payload) < 65536:
        header = struct.pack(">BBH", 0x81, 0x80 | 126, len(payload))
    else:
        header = struct.pack(">BBQ", 0x81, 0x80 | 127, len(payload))
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask_key + masked)


def _ws_recv(sock: socket.socket) -> dict:
    """Receive one WS frame and parse as JSON."""
    # Read frame header
    header = sock.recv(2)
    if len(header) < 2:
        raise ConnectionError("WS closed")
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]

    if masked:
        mask = sock.recv(4)

    payload = b""
    while len(payload) < length:
        chunk = sock.recv(min(length - len(payload), 65536))
        if not chunk:
            break
        payload += chunk

    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    if opcode == 0x1:  # text
        return json.loads(payload.decode())
    elif opcode == 0x8:  # close
        raise ConnectionError("WS close frame received")
    return {}


def _cdp_eval(js_expression: str) -> dict:
    """Navigate to ollama.com/settings and evaluate JS via CDP."""
    # Find or create an ollama.com tab
    tabs = json.loads(_http_get("/json/list"))
    ollama_tab = None
    for t in tabs:
        url = t.get("url", "")
        if "ollama.com" in url and "chrome-extension" not in url:
            ollama_tab = t
            break

    if not ollama_tab:
        # Chrome 149+ requires PUT (not GET) for /json/new
        import urllib.request
        url = f"http://127.0.0.1:{CDP_PORT}/json/new?{SETTINGS_URL}"
        req = urllib.request.Request(url, method="PUT")
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # tab may still have been created
        time.sleep(4)
        tabs = json.loads(_http_get("/json/list"))
        for t in tabs:
            if "ollama.com" in t.get("url", "") and "chrome-extension" not in t["url"]:
                ollama_tab = t
                break

    if not ollama_tab:
        return {"error": "no ollama tab"}

    sock = _ws_connect(ollama_tab["webSocketDebuggerUrl"])
    try:
        # Navigate to settings
        _ws_send(sock, {"id": 0, "method": "Page.navigate", "params": {"url": SETTINGS_URL}})
        time.sleep(1)

        # Drain nav response
        try:
            _ws_recv(sock)
        except Exception:
            pass

        # The settings page is a client-rendered app. Poll its text instead of
        # trusting a fixed sleep: on a busy Chrome the shell can appear several
        # seconds before the usage cards, which used to erase the last good
        # reading and make capacity fall back to a fake free-tier 100%.
        deadline = time.monotonic() + 15
        request_id = 1
        latest = {}
        while time.monotonic() < deadline:
            _ws_send(sock, {
                "id": request_id,
                "method": "Runtime.evaluate",
                "params": {"expression": js_expression},
            })
            for _ in range(20):
                msg = _ws_recv(sock)
                if msg.get("id") != request_id:
                    continue
                value = msg.get("result", {}).get("result", {}).get("value", "{}")
                latest = json.loads(value)
                break

            text = latest.get("text") or ""
            parsed = _parse(text)
            if (latest.get("signin")
                    or "signin.ollama.com" in (latest.get("url") or "")
                    or parsed["session_pct"] is not None
                    or parsed["weekly_pct"] is not None):
                return latest
            request_id += 1
            time.sleep(1)

        return latest or {"error": "usage cards did not load before timeout"}
    finally:
        sock.close()


def _parse(text: str) -> dict:
    """Parse usage percentages from page text."""
    out = {"session_pct": None, "weekly_pct": None, "tier": None}

    for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%\s*used", text):
        pct = float(m.group(1))
        ctx = text[max(0, m.start() - 80): m.end() + 20]
        if out["session_pct"] is None and _SESSION_WORDS.search(ctx):
            out["session_pct"] = pct
        elif out["weekly_pct"] is None and _WEEKLY_WORDS.search(ctx):
            out["weekly_pct"] = pct

    if out["session_pct"] is None or out["weekly_pct"] is None:
        for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%", text):
            pct = float(m.group(1))
            ctx = text[max(0, m.start() - 80): m.end() + 20]
            if out["session_pct"] is None and _SESSION_WORDS.search(ctx):
                out["session_pct"] = pct
            elif out["weekly_pct"] is None and _WEEKLY_WORDS.search(ctx):
                out["weekly_pct"] = pct

    mt = _TIER_RE.search(text)
    if mt:
        out["tier"] = mt.group(1).lower()
    return out


def main() -> int:
    # Check if Chrome with CDP is running
    try:
        _http_get("/json/version")
    except Exception:
        _write({"ok": False, "reason": f"no chrome CDP on port {CDP_PORT}", "scraped_at": int(time.time())})
        print(f"Chrome with CDP not found on port {CDP_PORT}. "
              "Ensure clawdbot Chrome is running.", file=sys.stderr)
        return 4

    js = ("JSON.stringify({url:location.href,"
          "signin:/sign in|log in|continue with/i.test(document.body.innerText),"
          "text:document.body.innerText})")

    try:
        data = _cdp_eval(js)
    except Exception as e:
        _write({"ok": False, "reason": f"cdp error: {e}", "scraped_at": int(time.time())})
        print(f"CDP error: {e}", file=sys.stderr)
        return 3

    if "error" in data:
        _write({"ok": False, "reason": data["error"], "scraped_at": int(time.time())})
        print(f"Page error: {data['error']}", file=sys.stderr)
        return 3

    if data.get("signin") or "signin.ollama.com" in (data.get("url") or ""):
        _write({"ok": False, "reason": "not logged in", "scraped_at": int(time.time())})
        print("NOT LOGGED IN. Log into ollama.com in the clawdbot Chrome browser.",
              file=sys.stderr)
        return 2

    text = data.get("text") or ""
    parsed = _parse(text)
    ok = parsed["session_pct"] is not None or parsed["weekly_pct"] is not None

    # Find the section headers' positions so we match the reset line that
    # follows the right header, not just the first reset in the whole page.
    session_header = re.search(r"\bsession\s+usage\b", text, re.I)
    weekly_header = re.search(r"\bweekly\s+usage\b", text, re.I)

    session_resets = None
    weekly_resets = None
    if session_header:
        section = text[session_header.start():session_header.start() + 500]
        sr = re.search(r"resets?\s*in\s*(.+?)(?:\n|$)", section, re.I | re.DOTALL)
        if sr:
            session_resets = sr.group(1).strip()
    if weekly_header:
        section = text[weekly_header.start():weekly_header.start() + 500]
        wr = re.search(r"resets?\s*in\s*(.+?)(?:\n|$)", section, re.I | re.DOTALL)
        if wr:
            weekly_resets = wr.group(1).strip()

    payload = {
        "ok": ok,
        "scraped_at": int(time.time()),
        "source_url": SETTINGS_URL,
        "session_pct": parsed["session_pct"],
        "weekly_pct": parsed["weekly_pct"],
        "session_resets_at": session_resets,
        "weekly_resets_at": weekly_resets,
        "tier": parsed["tier"],
        "raw_text": text[:4000],
    }
    _write(payload)

    if not ok:
        print(f"logged in, but couldn't parse usage — inspect {STORE}", file=sys.stderr)
        return 3

    print(f"scraped: session={parsed['session_pct']}% weekly={parsed['weekly_pct']}% "
          f"tier={parsed['tier']} → {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
