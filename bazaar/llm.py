"""Optional Claude access over plain HTTPS (no SDK needed).

Bazaar never lets a model decide anything that moves money. Claude is used only
to read a messy request when the deterministic parser leaves a field empty, and
every value it returns is validated in code before use. Without
ANTHROPIC_API_KEY everything still runs.
"""
import json
import os
import re
import urllib.request

MODEL = os.environ.get("BAZAAR_MODEL", "claude-sonnet-5-5")


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def complete(system: str, prompt: str, max_tokens: int = 500, timeout: int = 30):
    if not available():
        return None
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        from . import config
        with urllib.request.urlopen(req, timeout=timeout, context=config.ssl_context()) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    except Exception:
        return None  # the caller falls back to the deterministic path


def extract_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None
