#!/usr/bin/env python3
"""Open the McDonald's survey and automatically complete it with positive answers."""

from __future__ import annotations

import argparse
import base64
import ctypes
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

FILL_ANSWERS_SCRIPT = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : '';
  if (/thank you for (?:taking|completing)|survey (?:is )?complete|(?:validation|voucher) code(?: is|:)/i.test(bodyText)) {
    return {ok: true, done: true, action: 'completion page detected'};
  }

  const radios = [...document.querySelectorAll('input[type="radio"]:not([disabled])')];
  let answered = 0;
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
      answered++;
    }
  }

  const selects = [...document.querySelectorAll('select:not([disabled])')];
  let selected = 0;
  for (const sel of selects) {
    if (sel.value) continue;
    const opts = [...sel.options].filter(o => o.value);
    if (!opts.length) continue;
    sel.value = opts[opts.length - 1].value;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    selected++;
  }

  if (!radios.length && !selects.length) {
    return {ok: false, done: false, reason: 'No answer fields found'};
  }
  return {ok: true, done: false, action: 'filled answers', answered, selected};
})()
"""

ADVANCE_SCRIPT = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : '';
  if (/thank you for (?:taking|completing)|survey (?:is )?complete|(?:validation|voucher) code(?: is|:)/i.test(bodyText)) {
    return {ok: true, done: true, action: 'completion page detected'};
  }

  const clickables = [...document.querySelectorAll(
    'button:not([disabled]), input[type="submit"]:not([disabled]), input[type="button"]:not([disabled]), [role="button"]'
  )];
  const next = clickables.find(el =>
    /next|continue|submit|start/i.test(
      `${el.textContent || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''}`
    )
  );
  if (!next) return {ok: false, done: false, reason: 'No Start/Next button found'};
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


def get_survey_tab(
    debugging_port: int,
    previous_tab: dict | None = None,
    retries: int = 15,
    delay: float = 1.5,
    announce: bool = True,
) -> dict:
    """Find the survey tab, preferring its stable DevTools target ID."""
    if announce:
        print("Waiting for survey tab...", end="", flush=True)
    previous_id = previous_tab.get("id") if previous_tab else None
    for attempt in range(retries):
        try:
            targets = json.load(
                urlopen(f"http://127.0.0.1:{debugging_port}/json", timeout=3)
            )
            pages = [target for target in targets if target.get("type") == "page"]
            tab = next((target for target in pages if target.get("id") == previous_id), None)
            if not tab:
                tab = next(
                    (
                        target for target in pages
                        if "mcdfoodforthoughts" in target.get("url", "").lower()
                        or "food for thoughts" in target.get("title", "").lower()
                    ),
                    None,
                )
            if tab:
                if announce:
                    print(" found!")
                return tab
        except Exception:
            pass
        if announce:
            print(".", end="", flush=True)
        time.sleep(delay)
    if announce:
        print()
    raise RuntimeError(
        "Could not find the survey tab. Make sure Chrome opened and the survey page loaded."
    )


def cdp_command(tab: dict, method: str, params: dict | None = None) -> dict:
    """Send a Chrome DevTools command to a page target."""
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
        if not response.startswith(b"HTTP/1.1 101 ") or expected not in response:
            raise ConnectionError("WebSocket handshake failed")
        send_ws(conn, json.dumps({
            "id": 1,
            "method": method,
            "params": params or {},
        }))
        while True:
            payload = recv_ws(conn)
            if not payload:
                continue
            data = json.loads(payload)
            if data.get("id") != 1:
                continue
            if data.get("error"):
                raise RuntimeError(f"DevTools error: {data['error']}")
            return data.get("result", {})
    finally:
        conn.close()


def eval_js(tab: dict, expression: str, context_id: int | None = None) -> dict:
    """Run JS in the tab via Chrome DevTools and return the result."""
    params = {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": False,
    }
    if context_id is not None:
        params["contextId"] = context_id
    result = cdp_command(tab, "Runtime.evaluate", params)
    return result.get("result", {}).get("value") or {}


def iter_frame_contexts(tab: dict):
    """Yield an isolated JavaScript context for every frame in the tab."""
    frame_tree = cdp_command(tab, "Page.getFrameTree").get("frameTree", {})
    pending = [frame_tree]
    while pending:
        node = pending.pop(0)
        pending.extend(node.get("childFrames", []))
        frame = node.get("frame", {})
        frame_id = frame.get("id")
        if not frame_id:
            continue
        try:
            context = cdp_command(tab, "Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": "mcd-survey-helper",
            })
            yield frame, context["executionContextId"]
        except Exception:
            continue


def eval_js_in_frames(tab: dict, expression: str) -> dict:
    """Evaluate JS in each frame until one reports that it handled the action."""
    attempts = []
    for frame, context_id in iter_frame_contexts(tab):
        try:
            result = eval_js(tab, expression, context_id)
        except Exception as e:
            attempts.append({"url": frame.get("url", ""), "error": str(e)})
            continue
        if isinstance(result, dict) and result.get("ok"):
            return result
        attempts.append({"url": frame.get("url", ""), "result": result})
    return {"ok": False, "reason": "No matching survey form found", "frames": attempts}


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


def find_available_port() -> int:
    """Choose an unused local port for this Chrome session."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.bind(("127.0.0.1", 0))
        return conn.getsockname()[1]


def open_chrome(url: str, debugging_port: int) -> bool:
    exe = chrome_exe()
    if not exe:
        print("ERROR: Chrome not found.", file=sys.stderr)
        return False
    profile = tempfile.mkdtemp(prefix="mcd-survey-")
    subprocess.Popen([
        exe, "--incognito",
        f"--remote-debugging-port={debugging_port}",
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
    if len(code.replace("-", "")) != 12:
        raise ValueError("voucher code must contain 12 letters or numbers")
    return code


def normalize_price(raw: str) -> str:
    price = raw.strip().replace("£", "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", price):
        raise ValueError("price must look like 1.80")
    return f"{float(price):.2f}"


def fill_receipt_via_devtools(tab: dict, voucher_code: str, purchase_price: str) -> bool:
    """Fill receipt inputs in any page frame without requiring desktop focus."""
    pounds, pence = purchase_price.split(".")
    compact_code = voucher_code.replace("-", "")
    groups = [compact_code[i:i+4] for i in range(0, len(compact_code), 4)]
    values = [*groups, pounds, pence]
    fill_script = r"""
(() => {
  const values = __VALUES__;
  const ids = ['CN1', 'CN2', 'CN3', 'AmountSpent1', 'AmountSpent2'];
  const fields = ids.map(id => document.getElementById(id) || document.querySelector(`[name="${id}"]`));
  if (fields.some(field => !field)) {
    return {ok: false, reason: 'Receipt fields not found',
            fields: fields.filter(Boolean).length};
  }

  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (let i = 0; i < fields.length; i++) {
    const field = fields[i];
    field.focus();
    setter.call(field, values[i]);
    field.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
    field.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    field.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
  }
  return {ok: fields.every((field, index) => field.value === values[index]),
          action: 'filled receipt details', fields: fields.length};
})()
"""
    result = eval_js_in_frames(tab, fill_script.replace("__VALUES__", json.dumps(values)))
    print(f"Receipt fill result: {result}")
    return isinstance(result, dict) and bool(result.get("ok"))


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


# ── Hotkeys ──────────────────────────────────────────────────────────────────

def run_hotkeys(
    debugging_port: int, tab: dict, voucher_code: str, purchase_price: str
) -> None:
    """Watch for F8 and F9 presses without reserving global hotkeys."""
    user32 = ctypes.windll.user32
    vk_f8 = 0x77
    vk_f9 = 0x78

    print("\nHotkeys ready:")
    print("  Welcome page: press F9 once")
    print("  Receipt page: press F8 to fill details, then F9 to start")
    print("  Survey pages: press F8 to fill answers, then F9 to continue")
    print("Close this window or press Ctrl+C here to stop.")

    f8_was_down = False
    f9_was_down = False
    while True:
        f8_is_down = bool(user32.GetAsyncKeyState(vk_f8) & 0x8000)
        f9_is_down = bool(user32.GetAsyncKeyState(vk_f9) & 0x8000)
        try:
            if f8_is_down and not f8_was_down:
                tab = get_survey_tab(
                    debugging_port, tab, retries=4, delay=0.3, announce=False
                )
                if not fill_receipt_via_devtools(tab, voucher_code, purchase_price):
                    result = eval_js_in_frames(tab, FILL_ANSWERS_SCRIPT)
                    print(f"Fill result: {result}")
            elif f9_is_down and not f9_was_down:
                tab = get_survey_tab(
                    debugging_port, tab, retries=4, delay=0.3, announce=False
                )
                result = eval_js_in_frames(tab, ADVANCE_SCRIPT)
                print(f"Next result: {result}")
                if isinstance(result, dict) and result.get("done"):
                    print("Survey complete. Check Chrome for your voucher code.")
        except Exception as e:
            print(f"Hotkey action failed: {e}")
        f8_was_down = f8_is_down
        f9_was_down = f9_is_down
        time.sleep(0.05)


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

    debugging_port = find_available_port()
    if not open_chrome(args.url, debugging_port):
        input("Press Enter to exit.")
        return 1

    print("Waiting for Chrome to load the survey page...")
    time.sleep(4)

    try:
        tab = get_survey_tab(debugging_port)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        input("Press Enter to exit.")
        return 1

    if args.diagnose:
        input("Navigate to a page with radio buttons, then press Enter here to dump...")
        run_diagnose(tab)
        input("Press Enter to exit.")
        return 0

    try:
        run_hotkeys(debugging_port, tab, voucher_code, purchase_price)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        input("Press Enter to exit.")
        return 1

    input("\nPress Enter to exit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
