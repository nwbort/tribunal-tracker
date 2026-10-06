#!/usr/bin/env python3
"""
Browser-based scraper for the Australian Competition Tribunal website.

Plain curl only ever sees Cloudflare's "Just a moment..." interstitial because
the challenge requires a real browser to execute JavaScript (and sometimes
click a Turnstile checkbox). We drive a real Chrome via nodriver, wait for the
challenge to clear, and then, in the same browser session:

1. parse the /current-matters page into current-matters.json, and
2. for each matter listed in matters.txt, parse its filings table into
   matters/<slug>/documents.json and download the linked documents into
   matters/<slug>/documents/.

Anything new (a new current matter, or documents added/changed/removed in a
tracked matter) is pushed to ntfy.sh; see notify.py.

We launch Chrome ourselves (with a remote-debugging port) and wait until the
DevTools endpoint is actually ready before attaching nodriver. nodriver's own
launcher only waits ~2.5s for the port, which loses a race against Chrome's
~3s cold start on CI runners, so we manage the process and the readiness wait.

Usage: scrape.py [SLUG ...]     (defaults to the slugs in matters.txt)
"""

import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from shutil import which

import nodriver as uc

import notify
from parse import (
    BASE_URL,
    has_documents_table,
    parse_current_matters,
    parse_documents,
    parse_matter_heading,
)

CURRENT_MATTERS_URL = f"{BASE_URL}/current-matters"
CURRENT_MATTERS_JSON = "current-matters.json"
MATTERS_FILE = "matters.txt"
MATTERS_DIR = "matters"

# Markers that indicate we're still looking at the Cloudflare challenge rather
# than the real page.
CHALLENGE_MARKERS = (
    "Just a moment",
    "challenge-platform",
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "Verifying you are human",
)

MAX_WAIT_SECONDS = 90
# How often to re-check a page while it loads or the challenge runs, and how
# often to try clicking the Turnstile checkbox while a challenge is showing.
POLL_SECONDS = 0.5
TURNSTILE_CLICK_INTERVAL = 3

# nodriver's default args, which help the browser look like a normal user
# session rather than an automated one.
CHROME_ARGS = [
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--no-sandbox",  # CI runs as root
]


def looks_like_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def read_matters() -> list[str]:
    with open(MATTERS_FILE, encoding="utf-8") as f:
        lines = (line.split("#", 1)[0].strip() for line in f)
        return [line for line in lines if line]


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
    ):
        path = which(candidate)
        if path:
            return path
    raise FileNotFoundError("Could not find a Chrome/Chromium binary")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_chrome(chrome_path: str, port: int, user_data_dir: str):
    args = [
        chrome_path,
        *CHROME_ARGS,
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    # Chrome ignores the proxy environment variables, so pass any HTTPS proxy
    # on explicitly (not needed on GitHub's runners).
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")
    print(f"Launching Chrome: {chrome_path} (port {port})", flush=True)
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def wait_for_devtools(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.load(r)
                print(f"DevTools ready: {data.get('Browser')}", flush=True)
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def try_click_turnstile(tab):
    """Best-effort click of the Cloudflare Turnstile / 'Verify you are human'
    checkbox. Managed challenges often auto-clear, but some render a checkbox
    that must be clicked."""
    for text in ("Verify you are human", "Verify you are a human", "human"):
        try:
            el = await tab.find(text, best_match=True, timeout=3)
            if el:
                await el.mouse_click()
                print(f"  clicked element matching '{text}'", flush=True)
                return True
        except Exception:
            pass
    try:
        iframe = await tab.find("challenges.cloudflare.com", best_match=True, timeout=3)
        if iframe:
            await iframe.mouse_click()
            print("  clicked cloudflare iframe", flush=True)
            return True
    except Exception:
        pass
    return False


async def fetch_page(browser, url: str):
    """Load url and wait for the Cloudflare challenge to clear. Returns
    (tab, html), or (tab, None) if the challenge never cleared."""
    print(f"Navigating to {url}", flush=True)
    tab = await browser.get(url)

    target = url.split("?")[0].split("#")[0].rstrip("/")
    deadline = time.time() + MAX_WAIT_SECONDS
    next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    attempt = 0
    last_title = None
    while time.time() < deadline:
        attempt += 1
        try:
            # Only read the page once the tab has actually moved to url (it
            # still shows the previous page until the navigation commits) and
            # has fully loaded, so we never parse a half-received documents
            # table.
            state = await tab.evaluate(
                "document.readyState + ' ' + location.origin + location.pathname"
            )
            ready, _, href = str(state).partition(" ")
            on_page = href.rstrip("/") == target
            html = (
                await tab.get_content() if on_page and ready == "complete" else None
            )
        except Exception as e:
            print(f"  page check failed: {e}", flush=True)
            await tab.sleep(POLL_SECONDS)
            continue

        if html is not None:
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m:
                title = m.group(1).strip()
            challenge = looks_like_challenge(html)
            if not challenge or title != last_title:
                print(
                    f"  attempt {attempt}: {len(html)} bytes, title={title!r}",
                    flush=True,
                )
                last_title = title
            if not challenge:
                return tab, html

        if html is not None and time.time() >= next_click:
            await try_click_turnstile(tab)
            next_click = time.time() + TURNSTILE_CLICK_INTERVAL

        await tab.sleep(POLL_SECONDS)

    print(
        f"WARNING: page did not load or challenge did not clear within timeout"
        f" for {url}",
        flush=True,
    )
    return tab, None


async def browser_fetch(tab, url: str):
    """Fetch url with the page's own fetch() and return the bytes (or None)."""
    js = (
        "(async () => {"
        f"  const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});"
        "  if (!r.ok) return 'ERR:' + r.status;"
        "  const b = new Uint8Array(await r.arrayBuffer());"
        "  let s = '';"
        "  for (let i = 0; i < b.length; i += 0x8000)"
        "    s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));"
        "  return btoa(s);"
        "})()"
    )
    try:
        result = await tab.evaluate(js, await_promise=True, return_by_value=True)
    except Exception as e:
        print(f"  FAILED to download {url} via browser: {e}", flush=True)
        return None
    if not isinstance(result, str) or result.startswith("ERR:"):
        print(f"  FAILED to download {url} via browser: {result}", flush=True)
        return None
    return base64.b64decode(result)


async def download_documents(browser, tab, documents, docs_dir: str, page_url: str) -> None:
    """Download each linked document into docs_dir, reusing the browser's
    Cloudflare cookies + user-agent so the requests aren't bounced back to the
    challenge. Files that already exist are left alone (the asset URLs are
    immutable), and anything that comes back looking like a challenge page is
    skipped rather than saved with a misleading extension."""
    if not documents:
        print("No documents to download.", flush=True)
        return

    os.makedirs(docs_dir, exist_ok=True)

    try:
        cookies = await browser.cookies.get_all()
    except Exception as e:
        print(f"  could not read cookies: {e}", flush=True)
        cookies = []
    cookie_header = "; ".join(
        f"{c.name}={c.value}" for c in cookies if getattr(c, "name", None)
    )

    try:
        user_agent = await tab.evaluate("navigator.userAgent")
    except Exception:
        user_agent = "Mozilla/5.0"

    headers = {"User-Agent": user_agent, "Referer": page_url}
    if cookie_header:
        headers["Cookie"] = cookie_header

    for doc in documents:
        url = doc["url"]
        filename = doc["url_gh"].rsplit("/", 1)[-1]
        dest = os.path.join(docs_dir, filename)
        if os.path.exists(dest):
            continue
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except Exception as e:
            # Cloudflare sometimes rejects the replayed cookies (they're tied
            # to the browser's TLS fingerprint), so fetch from inside the page.
            print(f"  direct download failed ({e}); fetching via browser", flush=True)
            data = await browser_fetch(tab, url)
            if data is None:
                continue
        if b"Just a moment" in data[:2048] or b"challenge-platform" in data[:8192]:
            print(
                f"  WARNING: {url} returned a Cloudflare challenge, not saving",
                flush=True,
            )
            continue
        with open(dest, "wb") as f:
            f.write(data)
        print(f"  downloaded {dest} ({len(data)} bytes)", flush=True)


async def scrape_current_matters(browser) -> bool:
    _, html = await fetch_page(browser, CURRENT_MATTERS_URL)
    if html is None:
        return False
    matters = parse_current_matters(html)
    if not matters:
        print("ERROR: found no current matters; not updating", flush=True)
        return False
    print(f"Parsed {len(matters)} current matters", flush=True)

    previous = load_json(CURRENT_MATTERS_JSON, None)
    if previous is not None:
        for m in notify.new_matters(previous["current_matters"], matters):
            notify.send(
                f"New matter: {m['number']}",
                m["title"] or m["number"],
                click=m["url"],
                tags=["scales"],
                priority=4,
            )
    write_json(CURRENT_MATTERS_JSON, {"current_matters": matters})
    return True


async def scrape_matter(browser, slug: str) -> bool:
    url = f"{BASE_URL}/current-matters/{slug}"
    matter_dir = os.path.join(MATTERS_DIR, slug)
    docs_dir = os.path.join(matter_dir, "documents")
    json_path = os.path.join(matter_dir, "documents.json")

    tab, html = await fetch_page(browser, url)
    if html is None:
        return False
    if not has_documents_table(html):
        print(f"ERROR: no documents table on {url}; not updating", flush=True)
        return False

    heading = parse_matter_heading(html)
    documents = parse_documents(html, docs_dir=docs_dir)
    print(f"Parsed {len(documents)} documents for {heading['number']}", flush=True)

    previous = load_json(json_path, None)
    if previous is not None and previous["documents"] and not documents:
        print("ERROR: table is now empty; assuming a bad page, not updating", flush=True)
        return False

    await download_documents(browser, tab, documents, docs_dir, url)

    if previous is not None:
        diff = notify.diff_documents(previous["documents"], documents)
        if any(diff.values()):
            name = heading["number"] or slug
            notify.send(
                notify.documents_title(name, diff),
                notify.documents_message(diff),
                click=url,
                tags=["page_facing_up"],
                priority=4 if diff["added"] else 3,
            )

    write_json(
        json_path,
        {"matter": {**heading, "url": url}, "documents": documents},
    )
    print(f"Wrote {len(documents)} documents to {json_path}", flush=True)
    return True


async def scrape(slugs: list[str]) -> int:
    chrome_path = find_chrome()
    port = free_port()
    user_data_dir = tempfile.mkdtemp(prefix="cf-scrape-")
    proc = launch_chrome(chrome_path, port, user_data_dir)

    if not wait_for_devtools(port):
        print("ERROR: Chrome DevTools endpoint never became ready", flush=True)
        proc.terminate()
        return 3

    browser = await uc.start(
        host="127.0.0.1", port=port, browser_executable_path=chrome_path
    )

    failures = []
    try:
        try:
            ok = await scrape_current_matters(browser)
        except Exception as e:
            print(f"ERROR scraping current matters: {e!r}", flush=True)
            ok = False
        if not ok:
            failures.append("current-matters")
        for slug in slugs:
            try:
                ok = await scrape_matter(browser, slug)
            except Exception as e:
                print(f"ERROR scraping {slug}: {e!r}", flush=True)
                ok = False
            if not ok:
                failures.append(slug)
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass

    if failures:
        print(f"Failed: {', '.join(failures)}", flush=True)
        return 2
    return 0


def main() -> int:
    slugs = sys.argv[1:] or read_matters()
    return uc.loop().run_until_complete(scrape(slugs))


if __name__ == "__main__":
    sys.exit(main())
