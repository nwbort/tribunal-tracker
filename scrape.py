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

The tribunal's own decisions (directions, determinations) don't link to a
file but to a page wrapping a Federal Court judgment. Those are followed to
the judgment's "Original Word Document" on the Federal Court's site and the
DOCX is saved instead; see download_judgment().

Anything new (a new current matter, or documents added/changed/removed in a
tracked matter) is pushed to ntfy.sh; see notify.py.

We launch Chrome ourselves (with a remote-debugging port) and wait until the
DevTools endpoint is actually ready before attaching nodriver. nodriver's own
launcher only waits ~2.5s for the port, which loses a race against Chrome's
~3s cold start on CI runners, so we manage the process and the readiness wait.

Usage: scrape.py [SLUG ...]     (defaults to the slugs in matters.txt)
"""

import asyncio
import base64
import binascii
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
from urllib.parse import unquote, urlparse

import nodriver as uc

import notify
from parse import (
    BASE_URL,
    has_documents_table,
    is_judgment_host_url,
    judgment_file_link,
    parse_current_matters,
    parse_documents,
    parse_matter_heading,
    safe_filename,
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

# Ceiling on any single nodriver/CDP call (navigate, get_content, evaluate,
# ...). A stalled CDP round-trip otherwise hangs the run until Actions kills
# it, since fetch_page's MAX_WAIT_SECONDS is only checked between calls.
CDP_CALL_TIMEOUT_SECONDS = 30
# tab.find()'s own timeout doesn't bound the call (it only checks it after a
# full search of every frame), and the Turnstile click is best-effort, so a
# miss has to be cheap.
TURNSTILE_FIND_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT = 60

# Cloudflare decides per request: a file that downloads first go on one run
# can be refused with a 403 (cf-mitigated: challenge) on the next, and asking
# again a few seconds later usually goes straight through. 404/410 aren't
# retried: a link to nothing will still be a link to nothing.
BROWSER_FETCH_ATTEMPTS = 3
BROWSER_FETCH_RETRY_SECONDS = 5
RETRYABLE_FETCH_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

# How long to wait for a judgment page to render its file link, and for the
# browser to report where that link redirects.
JUDGMENT_LINK_WAIT_SECONDS = 15
JUDGMENT_REDIRECT_WAIT_SECONDS = 20

# Extension for a downloaded judgment whose URL doesn't carry a usable one,
# keyed by the file's leading bytes.
_SNIFFED_EXTENSIONS = (
    (b"PK\x03\x04", ".docx"),
    (b"%PDF", ".pdf"),
    (b"{\\rtf", ".rtf"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
)
_JUDGMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".rtf"}

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


def is_challenge_payload(data: bytes) -> bool:
    return b"Just a moment" in data[:2048] or b"challenge-platform" in data[:8192]


async def with_timeout(coro, what: str, seconds: float = CDP_CALL_TIMEOUT_SECONDS):
    """Await coro, turning a hang into a TimeoutError after seconds."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{what} did not respond within {seconds}s")


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
            el = await with_timeout(
                tab.find(text, best_match=True, timeout=3),
                "tab.find",
                TURNSTILE_FIND_TIMEOUT_SECONDS,
            )
            if el:
                await with_timeout(el.mouse_click(), "mouse_click")
                print(f"  clicked element matching '{text}'", flush=True)
                return True
        except Exception:
            pass
    try:
        iframe = await with_timeout(
            tab.find("challenges.cloudflare.com", best_match=True, timeout=3),
            "tab.find",
            TURNSTILE_FIND_TIMEOUT_SECONDS,
        )
        if iframe:
            await with_timeout(iframe.mouse_click(), "mouse_click")
            print("  clicked cloudflare iframe", flush=True)
            return True
    except Exception:
        pass
    return False


async def fetch_page(browser, url: str, tab=None, same_host: bool = False):
    """Load url and wait for the Cloudflare challenge to clear. Returns
    (tab, html), or (tab, None) if the challenge never cleared.

    tab navigates that tab rather than the browser's main one (see
    download_judgment). With same_host, any page on url's host counts as
    arrived, for a home page that redirects somewhere else on the site."""
    print(f"Navigating to {url}", flush=True)
    try:
        if tab is None:
            tab = await with_timeout(browser.get(url), "browser.get")
        else:
            await with_timeout(tab.get(url), "tab.get")
    except Exception as e:
        print(f"  navigation failed: {e}", flush=True)
        return tab, None

    target = url.split("?")[0].split("#")[0].rstrip("/")
    if same_host:
        parsed = urlparse(url)
        target = f"{parsed.scheme}://{parsed.netloc}"
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
            state = await with_timeout(
                tab.evaluate(
                    "document.readyState + ' ' + location.origin + location.pathname"
                ),
                "tab.evaluate",
            )
            ready, _, href = str(state).partition(" ")
            href = href.rstrip("/")
            on_page = href.startswith(target) if same_host else href == target
            html = (
                await with_timeout(tab.get_content(), "tab.get_content")
                if on_page and ready == "complete"
                else None
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


async def browser_fetch_once(tab, url: str) -> tuple[bytes | None, str]:
    """Fetch url with the page's own fetch(). Returns (bytes, "") or
    (None, what went wrong)."""
    js = (
        "(async () => {"
        "  try {"
        f"    const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});"
        "    if (!r.ok) {"
        "      const m = r.headers.get('cf-mitigated');"
        "      return 'ERR:HTTP ' + r.status + (m ? ' (cf-mitigated: ' + m + ')' : '');"
        "    }"
        "    const b = new Uint8Array(await r.arrayBuffer());"
        "    let s = '';"
        "    for (let i = 0; i < b.length; i += 0x8000)"
        "      s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));"
        "    return 'OK:' + btoa(s);"
        "  } catch (e) {"
        "    return 'ERR:' + String(e);"
        "  }"
        "})()"
    )
    try:
        result = await with_timeout(
            tab.evaluate(js, await_promise=True, return_by_value=True),
            "tab.evaluate (fetch)",
            REQUEST_TIMEOUT,
        )
    except Exception as e:
        return None, f"evaluate raised {e}"
    if not isinstance(result, str) or not result.startswith("OK:"):
        if isinstance(result, str) and result.startswith("ERR:"):
            return None, result[4:]
        return None, f"unexpected result {type(result).__name__}"
    try:
        data = base64.b64decode(result[3:], validate=True)
    except (ValueError, binascii.Error) as e:
        return None, f"undecodable payload: {e}"
    if not data:
        return None, "no bytes returned"
    if is_challenge_payload(data):
        # A challenge served with a 200, which the status check can't catch.
        return None, "Cloudflare challenge instead of the file"
    return data, ""


def retryable_fetch_failure(detail: str) -> bool:
    """A failed fetch is retried unless it got a status known to be durable
    (a 404, say). A network error or CDP hiccup has no status: retry it."""
    status = re.match(r"HTTP (\d{3})", detail)
    return int(status.group(1)) in RETRYABLE_FETCH_STATUSES if status else True


async def browser_fetch(tab, url: str) -> bytes | None:
    """browser_fetch_once, repeated while the failure looks transient."""
    for attempt in range(1, BROWSER_FETCH_ATTEMPTS + 1):
        data, detail = await browser_fetch_once(tab, url)
        if data is not None:
            return data
        print(
            f"  browser fetch of {url} failed"
            f" (attempt {attempt}/{BROWSER_FETCH_ATTEMPTS}): {detail}",
            flush=True,
        )
        if attempt == BROWSER_FETCH_ATTEMPTS or not retryable_fetch_failure(detail):
            return None
        await asyncio.sleep(BROWSER_FETCH_RETRY_SECONDS)
    return None


async def download_documents(browser, tab, documents, docs_dir: str, page_url: str) -> None:
    """Download each linked document into docs_dir, reusing the browser's
    Cloudflare cookies + user-agent so the requests aren't bounced back to the
    challenge. Files that already exist are left alone (the asset URLs are
    immutable), and anything that comes back looking like a challenge page is
    skipped rather than saved with a misleading extension."""
    documents = [d for d in documents if d.get("url_gh")]
    if not documents:
        print("No documents to download.", flush=True)
        return

    os.makedirs(docs_dir, exist_ok=True)

    try:
        cookies = await with_timeout(browser.cookies.get_all(), "cookies.get_all")
    except Exception as e:
        print(f"  could not read cookies: {e}", flush=True)
        cookies = []
    cookie_header = "; ".join(
        f"{c.name}={c.value}" for c in cookies if getattr(c, "name", None)
    )

    try:
        user_agent = await with_timeout(
            tab.evaluate("navigator.userAgent"), "tab.evaluate"
        )
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
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = r.read()
            if is_challenge_payload(data):
                raise ValueError("got a Cloudflare challenge")
        except Exception as e:
            # Cloudflare sometimes rejects the replayed cookies (they're tied
            # to the browser's TLS fingerprint), so fetch from inside the page.
            # browser_fetch never returns a challenge page.
            print(f"  direct download failed ({e}); fetching via browser", flush=True)
            data = await browser_fetch(tab, url)
            if data is None:
                continue
        with open(dest, "wb") as f:
            f.write(data)
        print(f"  downloaded {dest} ({len(data)} bytes)", flush=True)


# --- Tribunal decisions (Federal Court judgments) ----------------------------
# The decision links on a matter page wrap a judgment page on the Federal
# Court's site (see parse.judgment_page_url). The wrapper can't relay the file
# (its own Word link answers "Content cannot be accessed"), so it comes from
# the Federal Court:
#
#   www.judgments.fedcourt.gov.au/judgments/Judgments/tribunals/acompt/2026/2026acompt0002
#     -> "Original Word Document" -> .../2026acompt0002/2026acompt0002
#     -> 302 to www.fedcourt.gov.au/file-store/Judgments/Tribunals/
#              Australian Competition Tribunal/2026/2026ACOMPT0002/2026ACOMPT0002.docx
#
# Both Federal Court hosts sit behind their own Cloudflare challenge, and the
# redirect crosses between them, so a fetch() from the judgment page is refused
# (CORS) and the destination is read from the browser's network events.


def sniff_extension(data: bytes) -> str | None:
    """A document extension for data by its leading bytes, or None if it
    isn't one (an HTML page, say)."""
    return next((e for magic, e in _SNIFFED_EXTENSIONS if data.startswith(magic)), None)


def judgment_filename(final_url: str, data: bytes) -> str | None:
    """Local filename for a downloaded judgment: the file-store basename
    (2026ACOMPT0002.docx), with an extension sniffed from the bytes when the
    URL doesn't carry a usable one."""
    name = os.path.basename(unquote(urlparse(final_url).path)).strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() not in _JUDGMENT_EXTENSIONS:
        ext = sniff_extension(data)
        if not ext:
            return None
        stem = name
    return safe_filename(stem + ext)


def follow_redirects(url: str, hops: dict[str, str]) -> str:
    """Where url ends up, given the redirect hops (from -> to) seen."""
    seen = {url}
    while url in hops and hops[url] not in seen:
        url = hops[url]
        seen.add(url)
    return url


async def wait_for_judgment_link(tab, page_url: str, html: str) -> str | None:
    """The judgment page's file link, re-reading the page until it renders."""
    deadline = time.time() + JUDGMENT_LINK_WAIT_SECONDS
    while True:
        link = judgment_file_link(html, page_url)
        if link or time.time() >= deadline:
            return link
        await tab.sleep(1)
        try:
            html = await with_timeout(tab.get_content(), "tab.get_content")
        except Exception:
            pass


async def resolve_redirect(tab, link: str) -> str | None:
    """Where link redirects to, read from the browser's network events.

    A fetch() of it from the page is refused once the redirect leaves the
    host (CORS), but not before Chrome has issued the redirected request,
    which DevTools reports along with the response that redirected it.
    Navigating to the link instead doesn't work: once the file's host is
    cleared, the file is a download and the tab never leaves the page."""
    hops: dict[str, str] = {}

    def on_request(event):
        if event.redirect_response is not None:
            hops[event.redirect_response.url] = event.request.url

    tab.add_handler(uc.cdp.network.RequestWillBeSent, on_request)
    await with_timeout(tab.send(uc.cdp.network.enable()), "network.enable")
    await browser_fetch_once(tab, link)
    deadline = time.time() + JUDGMENT_REDIRECT_WAIT_SECONDS
    while link not in hops and time.time() < deadline:
        await tab.sleep(0.5)
    # nodriver can't parse some of newer Chrome's network events and logs a
    # traceback for each, so stop listening as soon as the redirect is known.
    try:
        await with_timeout(tab.send(uc.cdp.network.disable()), "network.disable")
    except Exception:
        pass
    final_url = follow_redirects(link, hops)
    return final_url if final_url != link else None


async def fetch_by_visiting(tab, url: str) -> bytes | None:
    """Load url in tab so Cloudflare can show its challenge, then fetch it.

    A fetch() Cloudflare wants to challenge is refused outright with a 403,
    since there's nowhere to render the challenge. A top-level navigation can
    be challenged and cleared, after which the fetch goes through. Once it
    clears, the navigation becomes a download and the tab stays on the
    challenge page (or wherever it was), still on url's origin, so keep
    trying the fetch from there rather than waiting for the page to change."""
    print(f"  visiting {url} to clear its challenge", flush=True)
    try:
        await with_timeout(tab.get(url), "tab.get")
    except Exception as e:
        # A download is reported as an aborted navigation.
        print(f"  navigation to {url} ended: {e}", flush=True)
    deadline = time.time() + MAX_WAIT_SECONDS
    next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    detail = ""
    while time.time() < deadline:
        await tab.sleep(2)
        data, detail = await browser_fetch_once(tab, url)
        if data is not None:
            return data
        if time.time() >= next_click:
            await try_click_turnstile(tab)
            next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    print(f"  WARNING: could not fetch {url}: {detail}", flush=True)
    return None


async def download_judgment(browser, page_url: str) -> tuple[str, bytes] | None:
    """Fetch the file behind a Federal Court judgment page.

    Works in a tab of its own, so the matter page is left as it was:

    1. load the judgment page (clearing that host's challenge) and find its
       file link;
    2. fetch that link from the page, which works if the file is served from
       the same host;
    3. otherwise learn where it redirects, clear the challenge on that host
       by loading its home page, and fetch the file from there, loading the
       file's own URL first if the file store challenges it separately.

    Returns (filename, bytes), or None if the file couldn't be fetched."""
    print(f"Following decision to {page_url}", flush=True)
    side_tab = None
    try:
        side_tab = await with_timeout(
            browser.get("about:blank", new_tab=True), "browser.get"
        )
        tab, html = await fetch_page(browser, page_url, tab=side_tab)
        if html is None:
            return None

        link = await wait_for_judgment_link(tab, page_url, html)
        if link is None:
            print(f"  WARNING: no file link on {page_url}", flush=True)
            return None

        # Same host, no redirect: the page can fetch it directly. Anything
        # but a document (an HTML page) means the file lives elsewhere.
        final_url = link
        data, _ = await browser_fetch_once(tab, link)
        if data is None or sniff_extension(data) is None:
            final_url = await resolve_redirect(tab, link)
            if final_url is None:
                print(
                    f"  WARNING: {link} is neither a file nor a redirect to one",
                    flush=True,
                )
                return None
            if not is_judgment_host_url(final_url):
                print(
                    f"  WARNING: {link} redirects to {final_url}, which is not"
                    f" a Federal Court host",
                    flush=True,
                )
                return None
            origin = f"https://{urlparse(final_url).hostname}/"
            tab, home_html = await fetch_page(browser, origin, tab=tab, same_host=True)
            if home_html is None:
                return None
            data = await browser_fetch(tab, final_url)
            if data is None:
                data = await fetch_by_visiting(tab, final_url)
            if data is None:
                return None
            if sniff_extension(data) is None:
                print(f"  WARNING: {final_url} is not a document", flush=True)
                return None

        filename = judgment_filename(final_url, data)
        if filename is None:
            print(f"  WARNING: no safe filename for {final_url}", flush=True)
            return None
        return filename, data
    except Exception as e:
        print(f"  WARNING: following {page_url} failed: {e!r}", flush=True)
        return None
    finally:
        if side_tab is not None:
            try:
                await with_timeout(side_tab.close(), "tab.close")
            except Exception:
                pass


async def mirror_judgments(browser, documents, previous_docs, docs_dir: str, judgments: dict) -> None:
    """Fill in url_gh for each decision in documents, downloading its file
    into docs_dir.

    A decision mirrored on an earlier run is taken from the previous
    documents.json rather than re-followed through three challenged hosts
    every run. judgments caches this run's downloads by judgment page, so a
    decision filed in two matters (ACT 3 and ACT 4 of 2026 share theirs) is
    only followed once."""
    previous_url_gh = {d["url"]: d.get("url_gh") for d in previous_docs}
    for doc in documents:
        page_url = doc.get("judgment_url")
        if not page_url:
            continue
        url_gh = previous_url_gh.get(doc["url"]) or ""
        if os.path.splitext(url_gh)[1].lower() in _JUDGMENT_EXTENSIONS and os.path.exists(
            os.path.join(docs_dir, url_gh.rsplit("/", 1)[-1])
        ):
            doc["url_gh"] = url_gh
            continue
        if page_url not in judgments:
            judgments[page_url] = await download_judgment(browser, page_url)
        result = judgments[page_url]
        if result is None:
            continue
        filename, data = result
        dest = os.path.join(docs_dir, filename)
        if not os.path.exists(dest):
            os.makedirs(docs_dir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            print(f"  downloaded {dest} ({len(data)} bytes)", flush=True)
        doc["url_gh"] = f"/{docs_dir}/{filename}"


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


async def scrape_matter(browser, slug: str, judgments: dict) -> bool:
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
    await mirror_judgments(
        browser,
        documents,
        previous["documents"] if previous else [],
        docs_dir,
        judgments,
    )

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
        # This run's decision downloads, shared across matters (see
        # mirror_judgments).
        judgments: dict = {}
        for slug in slugs:
            try:
                ok = await scrape_matter(browser, slug, judgments)
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
