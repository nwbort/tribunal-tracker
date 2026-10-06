#!/usr/bin/env python3
"""
Work out what changed between two scrapes and push it to ntfy.sh.

Messages go to the topic in the NTFY_TOPIC environment variable (on the
server in NTFY_SERVER, default https://ntfy.sh). Without NTFY_TOPIC the
messages are only printed, which is handy when running locally.
"""

import json
import os
import urllib.request

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
# ntfy truncates message bodies at 4096 bytes; leave room for the "...".
MAX_MESSAGE_BYTES = 3900

# Fields compared when deciding whether a document already listed has changed.
DOC_FIELDS = ("date", "filed_by", "description", "confidentiality")


def describe_document(doc: dict) -> str:
    parts = [doc.get("date"), doc.get("filed_by"), doc.get("description")]
    text = " | ".join(p for p in parts if p and p != "-")
    if doc.get("confidentiality") and doc["confidentiality"] != "Non-confidential":
        text += f" ({doc['confidentiality']})"
    return text


def diff_documents(old: list[dict], new: list[dict]) -> dict:
    """Documents are keyed by URL: asset URLs are stable, while the other
    fields are the things that might get corrected."""
    old_by_url = {d["url"]: d for d in old}
    new_by_url = {d["url"]: d for d in new}
    added = [d for u, d in new_by_url.items() if u not in old_by_url]
    removed = [d for u, d in old_by_url.items() if u not in new_by_url]
    changed = [
        (old_by_url[u], d)
        for u, d in new_by_url.items()
        if u in old_by_url
        and any(old_by_url[u].get(f) != d.get(f) for f in DOC_FIELDS)
    ]
    return {"added": added, "removed": removed, "changed": changed}


def documents_message(diff: dict) -> str:
    lines = []
    if diff["added"]:
        lines.append("New:")
        lines += [f"+ {describe_document(d)}" for d in diff["added"]]
    if diff["changed"]:
        lines.append("Changed:")
        for old, new in diff["changed"]:
            lines.append(f"~ {describe_document(new)}")
            lines.append(f"  (was: {describe_document(old)})")
    if diff["removed"]:
        lines.append("Removed:")
        lines += [f"- {describe_document(d)}" for d in diff["removed"]]
    return "\n".join(lines)


def documents_title(matter: str, diff: dict) -> str:
    counts = []
    for key, word in (("added", "new"), ("changed", "changed"), ("removed", "removed")):
        if diff[key]:
            n = len(diff[key])
            counts.append(f"{n} {word}")
    return f"{matter}: " + ", ".join(counts) + " document(s)"


def new_matters(old: list[dict], new: list[dict]) -> list[dict]:
    old_urls = {m["url"] for m in old}
    return [m for m in new if m["url"] not in old_urls]


def truncate(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_MESSAGE_BYTES:
        return text
    return data[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore").rstrip() + "\n..."


def send(title: str, message: str, click: str | None = None, tags=(), priority=3) -> None:
    message = truncate(message)
    print(f"NOTIFY: {title}\n{message}", flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (NTFY_TOPIC not set, not sending)", flush=True)
        return
    # Publish as JSON rather than with headers so non-ASCII text (en dashes,
    # curly quotes) in titles survives.
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": list(tags),
    }
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "tribunal-tracker (+https://github.com/nwbort/tribunal-tracker)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        print(f"  FAILED to send notification: {e}", flush=True)
