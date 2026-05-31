#!/usr/bin/env python3
"""Open the McDonald's survey and automatically complete it with positive answers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import urlopen


DEFAULT_SURVEY_URL = "https://www.mcdfoodforthoughts.com/"
DEBUGGING_PORT = 9222

AUTO_ANSWER_SCRIPT = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : '';
  if (/thank you|survey complete|validation code|voucher code/i.test(bodyText)) {
    return {ok: true, done: true, action: 'completion page detected'};
  }

  const radios = [...document.querySelectorAll('input[type="radio"]:not([disabled])')];
  if (radios.length > 0) {
    const groups = {};
    for (const r of radios) {
      const name = r.name || r.id || '__unnamed__';
      if (!groups[name]) groups[name] = [];
      groups[name].push(r);
    }
    for (const [, group] of Object.entries(groups)) {
      if (group.some(r => r.checked)) continue;
      const best = group.slice().sort((a, b) => {
        const av = parseFloat(a.value), bv = parseFloat(b.value);
        if (!isNaN(av) && !isNaN(bv)) return bv - av;
        const pos = /highly satisfied|very satisfied|excellent|always|yes|positive/i;
        const aPos = pos.test(`${a.value} ${a.getAttribute('aria-label') || ''}`);
        const bPos = pos.test(`${b.value} ${b.getAttribute('aria-label') || ''}`);
        if (aPos && !bPos) return -1;
        if (!aPos && bPos) return 1;
        return 0;
      })[0];
      best.click();
      best.dispatchEvent(new Event('input', {bubbles: true}));
      best.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  const selects = [...document.querySelectorAll('select:not([disabled])')];
  for (const sel of selects) {
    if (sel.value) continue;
    const opts = [...sel.options].filter(o => o.value);
    if (!opts.length) continue;
    sel.value = opts[opts.length - 1].value;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
  }

  const clickables = [...document.querySelectorAll(
    'button:not([disabled]), input[type="submit"]:not([disabled]), input[type="button"]:not([disabled]), [role="button"]'
  )];
  const next = clickables.find(el =>
    /next|continue|submit|start/i.test(
      `${el.textContent || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''}`
    )
  );
  if (!next) return {ok: false, done: false, reason: 'No Next button found'};
  next.click();
  return {ok: true, done: false, action: `clicked: ${(next.textContent || next.value || '').trim()}`};
})()
"""

DUMP_HTML_SCRIPT = r"""
(() => {
  const iframes = [...document.querySelectorAll('iframe')];
  return {
    url: location.href,
    title: document.title,
    iframes: iframes.map(f => f.src || f.id || '(no src)'),
    inputs: [...document.querySelectorAll('input,button,select,label,[role]')]
      .map(el => ({
        tag: el.tagName,
        type: el.type || '',
        name: el.name || '',
        value: el.value || '',
        role: el.getAttribute('role') || '',
        class: (el.className || '').slice(0, 60),
        text: (el.textContent || '').trim().slice(0, 60),
        ariaLabel: el.getAttribute('aria-label') || '',
      })),
    bodyHTML: document.body ? document.body.innerHTML.slice(0, 10000) : '',
  };
})()
"""


# ── WebSocket / DevTools helpers ──────────────────────────────────────────────

def receive_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = conn.recv(size)
        if not chunk:
            raise ConnectionError("DevTools connection closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def send_ws(conn: socket.socket, message: str) -> None:
    payload = message.encode("utf-8")
    mask = secrets.token_bytes(4)
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(0x80 | len(payload))
    elif len(payload) < 65536:
        header.extend([0x80 | 126])
        header.extend(struct.pack("!H", len(payload)))
    else:
        header.extend([0x80 | 127])
        header.extend(struct.pack("!Q", len(payload)))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    conn.sendall(bytes(header) + mask + masked)


def recv_ws(conn: socket.socket) -> str:
    first, second = receive_exact(conn, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", receive_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(conn, 8))[0]
    mask = receive_exact(conn, 4) if second & 0x80 else b""
    payload = receive_exact(conn, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if first & 0x0F != 0x01:
        return ""
    return payload.decode("utf-8")


def get_survey_tab(retries: int = 15, delay: float = 1.5) -> dict:
    """Wait for the mcdfoodforthoughts tab to appear in DevTools."""
    print("Waiting for survey tab...", end="", flush=True)
    for attempt in range(retries):
        try:
            targets = json.load(urlopen(f"http://127.0.0.1:{DEBUGGING_PORT}/json", timeout=3))
            tab = next(
                (t for t in targets
                 if t.get("type") == "page" and "mcdfoodforthoughts" in t.get("url", "")),
                None,
            )
            if tab:
                print(" found!")
                return tab
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(delay)
    print()
    raise RuntimeError(
        "Could not find the survey tab. Make sure Chrome opened and the survey page loaded."
    )


def eval_js(tab: dict, expression: str) -> dict:
    """Run JS in the tab via Chrome DevTools and return the result."""
    ws = urlparse(tab["webSocketDebuggerUrl"])
    conn = socket.create_connection((ws.hostname, ws.port), timeout=10)
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        handshake = (
            f"GET {ws.path} HTTP/1.1\r\n"
            f"Host: {ws.hostname}:{ws.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        conn.sendall(handshake.encode("ascii"))
        response = conn.recv(4096)
        expected = base64.b64encode(
            hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest()
        )
        if b"101 Switching Protocols" not in response or expected not in response:
            raise ConnectionError("WebSocket handshake failed")
        send_ws(conn, json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        data = json.loads(recv_ws(conn))
        return data.get("result", {}).get("result", {}).get("value") or {}
    finally:
        conn.close()


# ── Chrome launch ─────────────────────────────────────────────────────────────

def chrome_exe() -> str | None:
    pf = os.environ.get("ProgramFiles", "")
    lad = os.environ.get("LOCALAPPDATA", "")
    for path in [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
    ]:
        if os.path.isfile(path):
            return path
    return None


def open_chrome(url: str) -> bool:
    exe = chrome_exe()
    if not exe:
        print("ERROR: Chrome not found.", file=sys.stderr)
        return False
    profile = tempfile.mkdtemp(prefix="mcd-survey-")
    subprocess.Popen([
        exe, "--incognito",
        f"--remote-debugging-port={DEBUGGING_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        url,
    ])
    print("Chrome opened.")
    return True


# ── Receipt helpers ───────────────────────────────────────────────────────────

def normalize_code(raw: str) -> str:
    code = re.sub(r"\s+", "", raw.strip())
    if not code:
        raise ValueError("voucher code cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9-]+", code):
        raise ValueError("voucher code may only contain letters, numbers, and hyphens")
    return code


def normalize_price(raw: str) -> str:
    price = raw.strip().replace("£", "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", price):
        raise ValueError("price must look like 1.80")
    return f"{float(price):.2f}"


def fill_receipt_via_js(tab: dict, voucher_code: str, purchase_price: str) -> bool:
    """Fill the receipt entry form using JavaScript directly in the page."""
    pounds, pence = purchase_price.split(".")
    groups = voucher_code.split("-") if "-" in voucher_code else [
        voucher_code[i:i+4] for i in range(0, len(voucher_code), 4)
    ]
    fill_script = f"""
(() => {{
  // Fill code boxes — they're usually a series of text inputs
  const codeGroups = {json.dumps(groups)};
  const textInputs = [...document.querySelectorAll('input[type="text"], input:not([type])')];

  let filled = 0;
  for (let i = 0; i < codeGroups.length && i < textInputs.length; i++) {{
    const inp = textInputs[i];
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    nativeSetter.call(inp, codeGroups[i]);
    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
    filled++;
  }}

  // Fill price fields if present
  const priceInputs = textInputs.slice(codeGroups.length);
  if (priceInputs.length >= 2) {{
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    nativeSetter.call(priceInputs[0], {json.dumps(pounds)});
    priceInputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
    nativeSetter.call(priceInputs[1], {json.dumps(pence)});
    priceInputs[1].dispatchEvent(new Event('input', {{bubbles: true}}));
  }}

  // Click Start/Next
  const btn = [...document.querySelectorAll('button, input[type="submit"], input[type="button"]')]
    .find(b => /start|next|continue/i.test(b.textContent + b.value));
  if (btn) {{ btn.click(); return {{ok: true, filled}}; }}
  return {{ok: false, filled, reason: 'No start button found'}};
}})()
"""
    result = eval_js(tab, fill_script)
    if isinstance(result, dict) and result.get("ok"):
        print(f"Receipt details filled ({result.get('filled')} code boxes). Survey starting...")
        return True
    print(f"Receipt fill result: {result}")
    return False


# ── Diagnostic dump ───────────────────────────────────────────────────────────

def run_diagnose(tab: dict) -> None:
    print("\n── DIAGNOSTIC DUMP ──────────────────────────────────────────────")
    result = eval_js(tab, DUMP_HTML_SCRIPT)
    if not isinstance(result, dict):
        print(f"Unexpected: {result}")
        return
    print(f"URL   : {result.get('url')}")
    print(f"Title : {result.get('title')}")
    if result.get("iframes"):
        print(f"Iframes: {result['iframes']}")
    inputs = result.get("inputs", [])
    print(f"\n{len(inputs)} interactive elements:")
    for el in inputs:
        parts = [f"<{el['tag'].lower()}"]
        for k, v in [("type", el["type"]), ("name", el["name"]), ("role", el["role"]),
                     ("class", el["class"]), ("aria-label", el["ariaLabel"]), ("text", el["text"])]:
            if v:
                parts.append(f" {k}={v!r}")
        print("  " + "".join(parts) + ">")
    dump_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "survey_page_dump.html")
    with open(dump_path, "w", encoding="utf-8") as f:
        f.write(f"<!-- URL: {result.get('url')} -->\n{result.get('bodyHTML', '')}")
    print(f"\nHTML saved to: {dump_path}")
    print("─────────────────────────────────────────────────────────────────\n")


# ── Auto-complete loop ────────────────────────────────────────────────────────

def run_auto_complete(tab: dict, max_pages: int = 60, page_delay: float = 2.5) -> None:
    print("\nAuto-complete running. Do not close this window...")
    for page_num in range(1, max_pages + 1):
        time.sleep(page_delay)
        try:
            result = eval_js(tab, AUTO_ANSWER_SCRIPT)
        except Exception as e:
            print(f"  Page {page_num}: error — {e}, retrying...")
            time.sleep(2)
            try:
                result = eval_js(tab, AUTO_ANSWER_SCRIPT)
            except Exception as e2:
                print(f"  Page {page_num}: failed again — {e2}. Stopping.")
                return

        if not isinstance(result, dict):
            print(f"  Page {page_num}: unexpected response, skipping.")
            continue

        if result.get("done"):
            print(f"\n  Survey complete! Check Chrome for your voucher code.")
            return

        if result.get("ok"):
            print(f"  Page {page_num}: {result.get('action', 'answered and advanced')}")
        else:
            print(f"  Page {page_num}: {result.get('reason', 'unknown issue')} — trying anyway")

    print("Reached page limit without completing.")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatically complete the McDonald's Food for Thoughts survey."
    )
    parser.add_argument("code", nargs="?", help="voucher code (prompted if omitted)")
    parser.add_argument("--url", default=DEFAULT_SURVEY_URL)
    parser.add_argument("--diagnose", action="store_true",
                        help="dump page elements instead of auto-completing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    entered_code = args.code or input("Enter voucher code: ").strip()
    entered_price = input("Enter purchase price (e.g. 1.80): ").strip()

    try:
        voucher_code = normalize_code(entered_code)
        purchase_price = normalize_price(entered_price)
    except ValueError as e:
        print(f"Invalid input: {e}", file=sys.stderr)
        return 2

    if not open_chrome(args.url):
        input("Press Enter to exit.")
        return 1

    print("Waiting for Chrome to load the survey page...")
    time.sleep(4)

    try:
        tab = get_survey_tab()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        input("Press Enter to exit.")
        return 1

    if args.diagnose:
        input("Navigate to a page with radio buttons, then press Enter here to dump...")
        run_diagnose(tab)
        input("Press Enter to exit.")
        return 0

    # Fill receipt details via JS (no keyboard needed)
    print("\nFilling in receipt details...")
    time.sleep(1)
    fill_receipt_via_js(tab, voucher_code, purchase_price)

    # Wait for survey to transition past receipt page
    print("Waiting for survey questions to load...")
    time.sleep(4)

    run_auto_complete(tab)

    input("\nPress Enter to exit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
