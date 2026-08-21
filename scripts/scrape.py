#!/usr/bin/env python3
"""
Scrape every article linked from ../docs/*.md into local markdown for
personal preservation. Resumable, rate-limited, logs a manifest.

Usage:
    python scrape.py            # full run
    python scrape.py --limit 15 # test run (first 15 fetchable URLs)
    python scrape.py --workers 8
"""
import argparse
import concurrent.futures as cf
import csv
import hashlib
import os
import re
import threading
import time
from datetime import date
from urllib.parse import urlparse

import json
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import trafilatura
from trafilatura.settings import use_config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CFG = use_config()
CFG.set("DEFAULT", "USER_AGENTS", UA)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
DOCS = os.path.join(ROOT, "docs")
OUT = os.environ.get("SCRAPE_OUT", os.path.join(ROOT, "ext-sources"))
MANIFEST = os.path.join(OUT, "manifest.csv")
LOG = os.path.join(OUT, "scrape.log")
TODAY = date.today().isoformat()

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
AUTHOR_RE = re.compile(r"\)\s*[-–—]\s*by ([^.]+?)[.”]", re.I)

# URL substrings that should NOT be scraped (video/store/doc/social/binary).
STUB_HINTS = (
    "youtube.com", "youtu.be", "vimeo.com",
    "amazon.", "docs.google.", "drive.google.",
    "twitter.com", "//x.com", "slideshare.net",
)

_log_lock = threading.Lock()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        with open(LOG, "a") as f:
            f.write(line + "\n")


def norm_url(u):
    return u.rstrip(").,;” '\"")


def slugify(text, url):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    if not s:
        s = "source"
    h = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{s}-{h}"


def is_stub(url):
    low = url.lower()
    if low.endswith(".pdf"):
        return True
    return any(h in low for h in STUB_HINTS)


def collect_links():
    """Return ordered list of dicts, one per unique URL."""
    seen = {}
    order = []
    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".md"):
            continue
        topic = name[:-3]
        path = os.path.join(DOCS, name)
        with open(path, encoding="utf-8") as f:
            for line in f:
                for m in LINK_RE.finditer(line):
                    title = m.group(1).strip()
                    url = norm_url(m.group(2))
                    if not url.startswith("http"):
                        continue
                    if url in seen:
                        seen[url]["refs"].append(topic)
                        continue
                    am = AUTHOR_RE.search(line)
                    rec = {
                        "url": url,
                        "title": title,
                        "author": am.group(1).strip() if am else "",
                        "topic": topic,
                        "refs": [topic],
                        "note": line.strip().lstrip("- ").strip(),
                        "domain": urlparse(url).netloc,
                    }
                    seen[url] = rec
                    order.append(rec)
    return order


def yaml_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_file(rec, status, body):
    topic_dir = os.path.join(OUT, rec["topic"])
    os.makedirs(topic_dir, exist_ok=True)
    fp = os.path.join(topic_dir, slugify(rec["title"], rec["url"]) + ".md")
    fm = [
        "---",
        f'title: "{yaml_escape(rec["title"])}"',
        f'url: "{rec["url"]}"',
        f'author: "{yaml_escape(rec["author"])}"',
        f'topic: "{rec["topic"]}"',
        f'domain: "{rec["domain"]}"',
        f'referenced_in: "{", ".join(sorted(set(rec["refs"])))}"',
        f'fetched: "{TODAY}"',
        f'status: "{status}"',
        f'curator_note: "{yaml_escape(rec["note"])}"',
        "---",
        "",
    ]
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(fm))
        f.write(body or "")
        f.write("\n")
    return fp


def target_path(rec):
    return os.path.join(OUT, rec["topic"], slugify(rec["title"], rec["url"]) + ".md")


def already_ok(fp):
    if not os.path.exists(fp):
        return False
    try:
        with open(fp, encoding="utf-8") as f:
            head = f.read(600)
        return 'status: "ok"' in head or 'status: "stub"' in head
    except OSError:
        return False


def extract_md(url):
    """Fetch + extract to markdown. Returns md string or None."""
    dl = trafilatura.fetch_url(url, config=CFG)
    if not dl:
        return None
    md = trafilatura.extract(
        dl, output_format="markdown", include_links=True,
        include_images=False, include_comments=False, favor_precision=True,
        config=CFG,
    )
    return md if md and len(md) >= 200 else None


def wayback_snapshot(url, retries=4):
    """Return the closest archived snapshot URL, backing off on 429."""
    api = "https://archive.org/wayback/available?url=" + url
    for i in range(retries):
        try:
            resp = urlopen(Request(api, headers={"User-Agent": UA}), timeout=25)
            data = json.load(resp)
            snap = data.get("archived_snapshots", {}).get("closest", {})
            return snap.get("url") if snap.get("available") else None
        except HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (i + 1))
                continue
            return None
        except Exception:
            return None
    return None


def process(rec):
    fp = target_path(rec)
    if already_ok(fp):
        return (rec, "skip", fp)
    if is_stub(rec["url"]):
        body = (f"> **Not scraped** ({rec['domain']}). This is a video, book, "
                f"slide deck, shared doc, or social post — preserved as a "
                f"reference only.\n\n[Open original]({rec['url']})\n")
        write_file(rec, "stub", body)
        return (rec, "stub", fp)

    header = f"# {rec['title']}\n\n[Open original]({rec['url']})\n\n"
    try:
        md = extract_md(rec["url"])
        if md:
            write_file(rec, "ok", header + "---\n\n" + md)
            return (rec, "ok", fp)
        # fallback: Wayback Machine snapshot
        snap = wayback_snapshot(rec["url"])
        if snap:
            md = extract_md(snap)
            if md:
                note = f"[Archived copy]({snap})\n\n---\n\n"
                write_file(rec, "archived", header + note + md)
                return (rec, "archived", fp)
        write_file(rec, "failed",
                   f"> No readable content extracted from live site or Wayback "
                   f"(paywall / JS-rendered / bot-blocked).\n")
        return (rec, "failed", fp)
    except Exception as e:  # noqa
        write_file(rec, "failed", f"> Error: {e}\n")
        return (rec, "error", fp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    links = collect_links()
    total = len(links)
    log(f"Collected {total} unique URLs from {DOCS}")

    fetchable = [r for r in links if not is_stub(r["url"])]
    if args.limit:
        # test run: only the first N fetchable ones
        wanted = set(id(r) for r in fetchable[: args.limit])
        links = [r for r in links if id(r) in wanted]
        log(f"--limit {args.limit}: processing {len(links)} URLs")

    rows = []
    counts = {}
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, r): r for r in links}
        for fut in cf.as_completed(futs):
            rec, status, fp = fut.result()
            done += 1
            counts[status] = counts.get(status, 0) + 1
            rows.append((status, rec["topic"], rec["title"], rec["domain"],
                         rec["url"], os.path.relpath(fp, OUT)))
            if done % 25 == 0 or status in ("failed", "error"):
                log(f"{done}/{len(links)} [{status}] {rec['domain']} :: {rec['title'][:50]}")

    # rewrite manifest fresh (full runs) or merge (limit runs)
    existing = []
    if args.limit and os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            existing = list(csv.reader(f))[1:]
    seen_urls = set(r[4] for r in rows)
    merged = rows + [tuple(e) for e in existing if len(e) > 4 and e[4] not in seen_urls]
    with open(MANIFEST, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "topic", "title", "domain", "url", "file"])
        w.writerows(merged)

    log(f"DONE. {counts} | manifest: {MANIFEST}")


if __name__ == "__main__":
    main()
