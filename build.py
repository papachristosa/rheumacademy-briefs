#!/usr/bin/env python3
"""
build.py - builds the AHP Week in Review site from the brief PDFs.

The finished PDF is the source of truth. To publish an issue, add
briefs/YYYY-MM-DD.pdf and push. A GitHub Action runs this script, which:

  1. reads each PDF and pulls out the title, the introduction, and the ten
     items (headline, text, AHP takeaway, source link);
  2. saves that as issues/YYYY-MM-DD.json, so it can be corrected by hand;
  3. writes a preview page for the issue at /YYYY-MM-DD/ showing all ten
     headlines and a few items in full, with a subscribe button for the rest;
  4. rewrites index.html and feed.xml.

Nothing on the site links to the PDF. It stays at briefs/YYYY-MM-DD.pdf so
the link in the subscriber email keeps working, which means it is unlisted,
not locked: anyone who has the address can open it.

WHICH ITEMS ARE SHOWN IN FULL
  "open_items" in issues/YYYY-MM-DD.json, default [1, 2, 3]. Change the
  numbers and push; the page is rebuilt.

CORRECTING THE TEXT
  Replace the PDF. The JSON records the PDF's fingerprint and is re-read
  from the PDF when it changes ("open_items" is kept). To change the web
  text without touching the PDF, edit the JSON and set "locked": true so it
  is not re-read.

Run locally:  pip install pypdf  &&  python build.py
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIEFS = ROOT / "briefs"
ISSUES = ROOT / "issues"

SITE_URL = "https://briefs.rheumacademy.com"
SITE_NAME = "AHP Week in Review"
SUBSCRIBE_URL = "https://rheumacademy.kit.com/a6f4161949"
BYLINE = "Angelo Papachristos, PT, ACPAC"
TAGLINE = ("Ten new rheumatology papers every Friday, with the key numbers "
           "and one practical takeaway for allied health clinicians.")
DISCLAIMER = "Educational content for health professionals. Not medical advice."
DEFAULT_OPEN = [1, 2, 3]

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUM_RE = re.compile(r"^(\d{1,2})\.$")


# ════════════════════════════════════════════════════════════════
#  Reading the PDF
#
#  The brief is laid out by ReportLab with a fixed type scale, so each
#  piece of text is identified by its font size and weight, not by its
#  wording. That keeps the parser working when the header label, the
#  title prefix or the copy changes.
#
#      22 bold      title                 12 bold      item headline
#      16 bold      item number           10 regular   item text / takeaway
#      10.5 regular introduction          10 bold      "AHP Takeaway:"
#      10.5 oblique closing note          8.5          source name and link
#      9 and under  running header and footer (ignored), apart from 8.5
# ════════════════════════════════════════════════════════════════

def _chunks(pdf_path: Path) -> list:
    from pypdf import PdfReader
    out = []
    for page in PdfReader(str(pdf_path)).pages:
        def visit(text, cm, tm, font, size, out=out):
            t = (text or "").replace("\x7f", " ").strip()
            if not t:
                return
            name = str((font or {}).get("/BaseFont", "")) if font else ""
            eff = round(float(size) * abs(float(tm[0]) or 1.0) * 2) / 2
            out.append((eff, "Bold" in name, "Oblique" in name, t))
        page.extract_text(visitor_text=visit)
    return out


def _join(lines: list) -> str:
    text = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if text.endswith("-") and ln[:1].islower():
            text += ln                      # a word broken at its hyphen
        else:
            text += (" " if text else "") + ln
    return re.sub(r"\s+", " ", text).strip()


def parse_pdf(pdf_path: Path) -> dict:
    """The issue as structured data. Raises ValueError when the PDF does not
    look like a brief, so a bad upload fails the build instead of publishing
    a broken page."""
    title, subtitle, intro, closing = [], [], [], []
    items, cur, mode = [], None, "front"

    for size, bold, oblique, text in _chunks(pdf_path):
        if size >= 20 and bold:
            title.append(text)
        elif size == 16 and bold and _NUM_RE.match(text):
            cur = {"number": int(_NUM_RE.match(text).group(1)), "heading": [],
                   "body": [], "takeaway": [], "source": [], "url": ""}
            items.append(cur)
            mode = "body"
        elif size == 12 and bold and cur is not None:
            cur["heading"].append(text)
        elif size == 10 and bold and text.lower().startswith("ahp takeaway"):
            mode = "takeaway"
            rest = text.split(":", 1)[1].strip() if ":" in text else ""
            if rest and cur is not None:
                cur["takeaway"].append(rest)
        elif size == 10 and cur is not None and not bold:
            cur["takeaway" if mode == "takeaway" else "body"].append(text)
        elif size == 8.5 and cur is not None:
            if text.lower().startswith("source"):
                rest = text.split(":", 1)[1].strip() if ":" in text else ""
                if rest:
                    cur["source"].append(rest)
            elif text.startswith("http"):
                cur["url"] = text.strip()
                mode = "after"
            else:
                cur["source"].append(text)
        elif size == 11 and not items:
            subtitle.append(text)
        elif size == 10.5 and not oblique and not items:
            intro.append(text)
        elif size == 10.5 and oblique and items:
            closing.append(text)

    full_title = _join(title)
    short = full_title.split(": ", 1)[1] if ": " in full_title else full_title
    issue = {
        "title": short,
        "pdf_title": full_title,
        "subtitle": _join(subtitle),
        "intro": _join(intro),
        "closing": _join(closing),
        "items": [{
            "number": it["number"],
            "heading": _join(it["heading"]),
            "body": _join(it["body"]),
            "takeaway": _join(it["takeaway"]),
            "source": _join(it["source"]),
            "url": it["url"],
        } for it in items],
    }

    problems = []
    if not issue["title"]:
        problems.append("no title found")
    if len(issue["items"]) < 3:
        problems.append(f"only {len(issue['items'])} item(s) found")
    for n, it in enumerate(issue["items"], 1):
        if it["number"] != n:
            problems.append(f"item {n} is numbered {it['number']}")
        for field in ("heading", "body", "takeaway"):
            if len(it[field]) < 15:
                problems.append(f"item {n} has no {field}")
        if not it["url"].startswith("http"):
            problems.append(f"item {n} has no source link")
    if problems:
        raise ValueError(f"{pdf_path.name}: " + "; ".join(problems))
    return issue


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_issue(pdf_path: Path) -> dict:
    """Issue data for one PDF, read from issues/DATE.json when that is
    current, otherwise parsed from the PDF and saved."""
    day = pdf_path.stem
    json_path = ISSUES / f"{day}.json"
    fingerprint = _sha(pdf_path)
    saved = {}
    if json_path.exists():
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        if saved.get("locked") or saved.get("pdf_sha256") == fingerprint:
            saved["date"] = day
            return saved

    issue = parse_pdf(pdf_path)
    issue["date"] = day
    issue["pdf_sha256"] = fingerprint
    issue["locked"] = False
    issue["open_items"] = saved.get("open_items") or list(DEFAULT_OPEN)
    ISSUES.mkdir(exist_ok=True)
    json_path.write_text(
        json.dumps(issue, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"  parsed {pdf_path.name}: {len(issue['items'])} items")
    return issue


def ensure_thumbnail(pdf_path: Path) -> bool:
    """briefs/DATE.jpg, page 1 of the PDF, made with pdftoppm if missing."""
    jpg = pdf_path.with_suffix(".jpg")
    if jpg.exists():
        return True
    try:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "80", "-f", "1", "-l", "1",
             "-singlefile", str(pdf_path), str(pdf_path.with_suffix(""))],
            check=True, capture_output=True, timeout=120)
        return jpg.exists()
    except Exception as e:                                # noqa: BLE001
        print(f"  no thumbnail for {pdf_path.name} ({e})")
        return False


# ════════════════════════════════════════════════════════════════
#  Writing the pages
# ════════════════════════════════════════════════════════════════

CSS = """
  :root { --navy:#1b2a4a; --teal:#2a9d8f; --teal-d:#1f7f74; --mint:#e0f5f1; --body:#333; --grey:#5f6670; --rule:#d9dee5; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:Helvetica, Arial, sans-serif; color:var(--body); background:#f7f8fa; line-height:1.6; }
  a { color:var(--teal-d); }
  .skip { position:absolute; left:-9999px; }
  .skip:focus { left:12px; top:12px; background:#fff; padding:8px 12px; border-radius:4px; z-index:5; }
  header { background:var(--navy); color:#fff; }
  .bar { max-width:960px; margin:0 auto; padding:14px 20px; display:flex; justify-content:space-between; align-items:center; gap:12px; font-size:13px; }
  .bar a { color:#fff; text-decoration:none; }
  .bar b { letter-spacing:.04em; }
  .bar span { color:var(--mint); font-style:italic; }
  .hero { max-width:960px; margin:0 auto; padding:40px 20px 28px; display:flex; gap:28px; align-items:center; }
  .hero img { width:96px; height:auto; border-radius:6px; flex:none; }
  h1 { color:var(--navy); font-size:30px; line-height:1.2; margin:0 0 8px; }
  .lede { font-size:17px; margin:0 0 6px; }
  .by { color:var(--grey); font-size:14px; margin:0; }
  .cta { max-width:960px; margin:0 auto 32px; padding:0 20px; }
  .cta div, .panel { background:var(--mint); border:1px solid var(--teal); border-radius:6px; padding:18px 20px; display:flex; flex-wrap:wrap; gap:14px; align-items:center; justify-content:space-between; }
  .cta p, .panel p { margin:0; max-width:620px; }
  .btn { display:inline-block; background:var(--teal-d); color:#fff; text-decoration:none; font-weight:bold; padding:11px 20px; border-radius:5px; white-space:nowrap; }
  .btn:hover { background:#186a61; }
  .btn.ghost { background:#fff; color:var(--teal-d); border:1px solid var(--teal); }
  .btn.ghost:hover { background:var(--mint); }
  main { max-width:960px; margin:0 auto; padding:0 20px 40px; }
  h2 { color:var(--navy); font-size:20px; border-bottom:2px solid var(--teal); padding-bottom:6px; }
  .issue { background:#fff; border:1px solid var(--rule); border-radius:6px; padding:20px; display:flex; gap:22px; margin-bottom:20px; }
  .issue img { width:190px; height:auto; border:1px solid var(--rule); flex:none; }
  .issue h3 { margin:0 0 4px; color:var(--navy); font-size:18px; }
  .issue h3 a { color:inherit; text-decoration:none; }
  .issue h3 a:hover { text-decoration:underline; }
  .date { color:var(--grey); font-size:13px; margin:0 0 10px; }
  .issue ol { margin:0 0 16px; padding-left:20px; font-size:14px; }
  .issue ol li { margin-bottom:2px; }
  .actions { display:flex; flex-wrap:wrap; gap:10px; }
  article.brief { background:#fff; border:1px solid var(--rule); border-radius:6px; padding:30px 34px; }
  .crumb { font-size:13px; margin:22px 0 14px; }
  .brief h1 { font-size:28px; }
  .intro { font-size:16.5px; margin:18px 0 26px; }
  .toc { border:1px solid var(--rule); border-radius:6px; padding:16px 20px 16px 8px; margin:0 0 26px; background:#fbfcfd; }
  .toc h2 { border:0; padding:0 0 0 12px; margin:0 0 8px; font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:var(--grey); }
  .toc ol { margin:0; padding-left:34px; }
  .toc li { margin:5px 0; }
  .toc .tag { font-size:11.5px; color:var(--grey); border:1px solid var(--rule); border-radius:10px; padding:1px 8px; margin-left:6px; white-space:nowrap; }
  .toc .tag.open { color:var(--teal-d); border-color:var(--teal); }
  .item { border-top:1px solid var(--rule); padding-top:22px; margin-top:24px; }
  .item h2 { border:0; padding:0; margin:0 0 8px; font-size:20px; line-height:1.3; }
  .item h2 .n { color:var(--teal); font-size:24px; margin-right:6px; }
  .take { background:var(--mint); border:1px solid var(--teal); border-radius:4px; padding:12px 16px; margin:14px 0 10px; }
  .take b { color:var(--teal-d); }
  .src { font-size:13.5px; color:var(--grey); margin:0; }
  .panel { margin-top:30px; }
  footer { border-top:1px solid var(--rule); color:var(--grey); font-size:12px; text-align:center; padding:20px; }
  @media (max-width:640px) {
    .hero { flex-direction:column; align-items:flex-start; padding-top:28px; }
    .issue { flex-direction:column; }
    .issue img { width:100%; max-width:320px; }
    h1 { font-size:25px; }
    .bar span { display:none; }
    article.brief { padding:22px 18px; }
    .brief h1 { font-size:23px; }
  }
"""


def e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def nice_date(day: str) -> str:
    y, m, d = (int(x) for x in _DATE_RE.match(day).groups())
    return date(y, m, d).strftime("%B %-d, %Y")


def _count_word(n: int) -> str:
    words = ["zero", "one", "two", "three", "four", "five", "six", "seven",
             "eight", "nine", "ten", "eleven", "twelve"]
    return words[n] if 0 <= n < len(words) else str(n)


def _head(title: str, description: str, url: str, image: str,
          prefix: str, extra: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en-CA">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="RheumAcademy">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{e(url)}">
<meta property="og:image" content="{e(image)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{prefix}assets/logo.png">
<link rel="alternate" type="application/rss+xml" title="{e(SITE_NAME)}" href="{SITE_URL}/feed.xml">
<style>{CSS}</style>
{extra}</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header>
  <div class="bar"><a href="{prefix or './'}"><b>RHEUMACADEMY &nbsp;|&nbsp; ARTHROS</b></a><span>Canada’s Rheumatology Education Platform</span></div>
</header>
"""


FOOT = f"""
<footer>
  RheumAcademy · Toronto, Ontario · <a href="https://www.rheumacademy.com">rheumacademy.com</a><br>
  {e(DISCLAIMER)}
</footer>
</body>
</html>
"""


def _open_numbers(issue: dict) -> list:
    valid = {it["number"] for it in issue["items"]}
    chosen = [n for n in (issue.get("open_items") or DEFAULT_OPEN) if n in valid]
    return chosen or sorted(valid)[:3]


def issue_page(issue: dict) -> str:
    day = issue["date"]
    url = f"{SITE_URL}/{day}/"
    image = f"{SITE_URL}/briefs/{day}.jpg"
    open_n = _open_numbers(issue)
    total = len(issue["items"])
    closed = total - len(open_n)
    description = (f"{_count_word(total).capitalize()} rheumatology papers for "
                   f"allied health clinicians, week ending {nice_date(day)}: "
                   + "; ".join(it["heading"] for it in issue["items"][:3]) + ".")

    ld = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": issue["title"], "datePublished": day,
        "author": {"@type": "Person", "name": "Angelo Papachristos"},
        "publisher": {"@type": "Organization", "name": "RheumAcademy",
                      "url": "https://www.rheumacademy.com"},
        "isAccessibleForFree": True, "mainEntityOfPage": url,
    }
    extra = ('<script type="application/ld+json">'
             + json.dumps(ld, ensure_ascii=False) + "</script>\n")

    toc = []
    for it in issue["items"]:
        if it["number"] in open_n:
            toc.append(f'<li><a href="#item-{it["number"]}">{e(it["heading"])}</a>'
                       f'<span class="tag open">read below</span></li>')
        else:
            toc.append(f'<li>{e(it["heading"])}'
                       f'<span class="tag">in the full brief</span></li>')

    blocks = []
    for it in issue["items"]:
        if it["number"] not in open_n:
            continue
        source = e(it["source"]) or "Source"
        blocks.append(f"""
    <section class="item" id="item-{it['number']}">
      <h2><span class="n">{it['number']}.</span>{e(it['heading'])}</h2>
      <p>{e(it['body'])}</p>
      <p class="take"><b>AHP Takeaway:</b> {e(it['takeaway'])}</p>
      <p class="src"><b>Source:</b> {source} · <a href="{e(it['url'])}" rel="noopener">{e(it['url'])}</a></p>
    </section>""")

    if closed > 0:
        more = (f"<b>{_count_word(closed).capitalize()} more "
                f"{'finding is' if closed == 1 else 'findings are'} in the full "
                f"brief.</b> Subscribe free and the complete PDF arrives by "
                f"email every Friday.")
    else:
        more = ("<b>Get the next one by email.</b> Subscribe free and the "
                "complete PDF arrives every Friday.")

    return _head(f"{issue['title']} | {SITE_NAME}", description, url, image,
                 "../", extra) + f"""
<main id="main">
  <p class="crumb"><a href="../">← All briefs</a></p>
  <article class="brief">
    <p class="date">{e(SITE_NAME)} · {nice_date(day)} · {total} papers</p>
    <h1>{e(issue['title'])}</h1>
    <p class="by">By {e(BYLINE)}</p>
    <p class="intro">{e(issue['intro'])}</p>

    <nav class="toc" aria-label="In this issue">
      <h2>In this issue</h2>
      <ol>
        {chr(10).join('        ' + t for t in toc).strip()}
      </ol>
    </nav>
{''.join(blocks)}

    <div class="panel">
      <p>{more}</p>
      <a class="btn" href="{SUBSCRIBE_URL}">Subscribe free</a>
    </div>
  </article>
</main>
""" + FOOT


def index_page(issues: list) -> str:
    cards = []
    for issue in issues:
        day = issue["date"]
        has_jpg = (BRIEFS / f"{day}.jpg").exists()
        img = (f'<a href="{day}/"><img src="briefs/{day}.jpg" alt="Page 1 of '
               f'the {nice_date(day)} brief" loading="lazy"></a>'
               if has_jpg else "")
        heads = "\n".join(f"        <li>{e(it['heading'])}</li>"
                          for it in issue["items"])
        cards.append(f"""
  <article class="issue">
    {img}
    <div>
      <h3><a href="{day}/">{e(issue['title'])}</a></h3>
      <p class="date">{nice_date(day)} · {len(issue['items'])} papers</p>
      <ol>
{heads}
      </ol>
      <p class="actions">
        <a class="btn" href="{day}/">Read the preview</a>
        <a class="btn ghost" href="{SUBSCRIBE_URL}">Get the full brief by email</a>
      </p>
    </div>
  </article>""")

    image = (f"{SITE_URL}/briefs/{issues[0]['date']}.jpg" if issues
             else f"{SITE_URL}/assets/logo.png")
    description = ("A free weekly evidence brief for allied health clinicians "
                   "in rheumatology: ten new papers every Friday, with the key "
                   "numbers and a takeaway for clinic.")
    return _head(f"RheumAcademy {SITE_NAME}", description, f"{SITE_URL}/",
                 image, "") + f"""
<section class="hero">
  <img src="assets/logo.png" alt="RheumAcademy logo">
  <div>
    <h1>{e(SITE_NAME)}</h1>
    <p class="lede">{e(TAGLINE)}</p>
    <p class="by">By {e(BYLINE)}</p>
  </div>
</section>

<section class="cta">
  <div>
    <p><b>Get the full brief by email every Friday.</b> All ten papers as a PDF, with the numbers and a takeaway for each. Free, and you can unsubscribe any time.</p>
    <a class="btn" href="{SUBSCRIBE_URL}">Subscribe free</a>
  </div>
</section>

<main id="main">
  <h2>Briefs</h2>
{''.join(cards)}
</main>
""" + FOOT


def feed(issues: list) -> str:
    entries = []
    for issue in issues[:30]:
        day = issue["date"]
        y, m, d = (int(x) for x in _DATE_RE.match(day).groups())
        when = format_datetime(datetime(y, m, d, 12, 0, tzinfo=timezone.utc))
        heads = "".join(f"<li>{e(it['heading'])}</li>" for it in issue["items"])
        body = (f"<p>{e(issue['intro'])}</p><ol>{heads}</ol>"
                f'<p><a href="{SITE_URL}/{day}/">Read the preview</a> or '
                f'<a href="{SUBSCRIBE_URL}">subscribe free</a> for the full '
                f"brief.</p>")
        entries.append(f"""  <item>
    <title>{e(issue['title'])}</title>
    <link>{SITE_URL}/{day}/</link>
    <guid isPermaLink="true">{SITE_URL}/{day}/</guid>
    <pubDate>{when}</pubDate>
    <description>{e(body)}</description>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>RheumAcademy {e(SITE_NAME)}</title>
  <link>{SITE_URL}/</link>
  <description>{e(TAGLINE)}</description>
  <language>en-ca</language>
{chr(10).join(entries)}
</channel>
</rss>
"""


def main() -> int:
    pdfs = sorted(p for p in BRIEFS.glob("*.pdf") if _DATE_RE.match(p.stem))
    if not pdfs:
        print("No briefs/YYYY-MM-DD.pdf files found.")
        return 1
    issues, failed = [], []
    for pdf in pdfs:
        try:
            issues.append(load_issue(pdf))
            ensure_thumbnail(pdf)
        except Exception as ex:                           # noqa: BLE001
            failed.append(str(ex))
    issues.sort(key=lambda i: i["date"], reverse=True)

    for issue in issues:
        out = ROOT / issue["date"]
        out.mkdir(exist_ok=True)
        (out / "index.html").write_text(issue_page(issue), encoding="utf-8")
    (ROOT / "index.html").write_text(index_page(issues), encoding="utf-8")
    (ROOT / "feed.xml").write_text(feed(issues), encoding="utf-8")
    (ROOT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built {len(issues)} issue(s).")

    if failed:
        print("\nCOULD NOT READ:")
        for f in failed:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
