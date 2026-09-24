"""
Small-cap 13D / 13G feed builder.

Pulls the latest SCHEDULE 13D and SCHEDULE 13G filings from EDGAR, looks up
each issuer's market cap, keeps only the ones under your cutoff, and writes:

    docs/13d.xml     RSS feed of small-cap 13D filings (+ amendments)
    docs/13g.xml     RSS feed of small-cap 13G filings (+ amendments)
    docs/index.html  a simple page listing everything, if you'd rather browse

Filings whose market cap can't be found (private companies, foreign issuers,
SPACs, brand-new tickers) are KEPT and marked "[Cap unknown]" so nothing
obscure slips past.

Settings come from environment variables (set them in GitHub, see the guide):
    SEC_USER_AGENT   required by the SEC, e.g. "Kyle Smith kyle@gmail.com"
    MAX_MARKET_CAP   cutoff in dollars, default 500000000 ($500M)

New 13Ds from a filer that previously reported the same stake on a 13G are
tagged "[13G→13D]". Two checks, either one is enough:
    1. the filer ticked the "previously filed on Schedule 13G" box on the
       13D cover page (previouslyFiledFlag in the XML), or
    2. EDGAR shows a 13G from the same filer on the same issuer in the last
       CONVERSION_LOOKBACK_YEARS years (backstop for filers who skip the box).
"""

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

# ----------------------------------------------------------------- settings
USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
MAX_CAP = float(os.environ.get("MAX_MARKET_CAP") or 500_000_000)
PAGES_PER_FORM = 5          # 5 pages x 100 = up to 500 recent filings per form
KEEP_DAYS = 45              # how long a filing stays in the feed
CAP_CACHE_DAYS = 5          # re-check a company's market cap after this many days
FEED_BASE = os.environ.get("FEED_BASE_URL", "")  # optional, for <link> tags
CONVERSION_LOOKBACK_YEARS = 3  # how far back to look for an earlier 13G

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "state.json"
DOCS = ROOT / "docs"

FORMS = {"13D": "SCHEDULE 13D", "13G": "SCHEDULE 13G"}
ATOM = "{http://www.w3.org/2005/Atom}"

# ----------------------------------------------------------------- helpers
_last_sec_call = 0.0


def sec_get(url: str) -> bytes:
    """GET from sec.gov, politely (the SEC allows max 10 requests/second)."""
    global _last_sec_call
    wait = 0.15 - (time.time() - _last_sec_call)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Encoding": "identity"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                _last_sec_call = time.time()
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print(f"  retrying {url} ({e})")
            time.sleep(2 * (attempt + 1))
    return b""


def fmt_cap(v):
    if v is None:
        return "cap unknown"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    return f"${v / 1e6:.1f}M"


# Entry titles look like:
#   "SCHEDULE 13G/A - Creatd, Inc. (0001357671) (Subject)"
TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)\s*$")


def fetch_current(form_type: str):
    """Return raw entries from EDGAR's 'latest filings' Atom feed."""
    entries = []
    for page in range(PAGES_PER_FORM):
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               f"&type={form_type.replace(' ', '+')}&company=&dateb=&owner=include"
               f"&start={page * 100}&count=100&output=atom")
        root = ET.fromstring(sec_get(url))
        page_entries = root.findall(f"{ATOM}entry")
        for e in page_entries:
            title = (e.findtext(f"{ATOM}title") or "").strip()
            m = TITLE_RE.match(title)
            if not m:
                continue
            link_el = e.find(f"{ATOM}link")
            summary = e.findtext(f"{ATOM}summary") or ""
            acc = re.search(r"\d{10}-\d{2}-\d{6}", (e.findtext(f"{ATOM}id") or "") + summary)
            if not acc:
                continue
            entries.append({
                "form": m["form"].strip(),
                "name": m["name"].strip(),
                "cik": m["cik"],
                "role": m["role"].strip(),
                "acc": acc.group(0),
                "link": link_el.get("href") if link_el is not None else "",
                "updated": e.findtext(f"{ATOM}updated") or "",
            })
        if len(page_entries) < 100:
            break
    return entries


def group_by_filing(entries):
    """EDGAR lists each filing twice: once for the subject company (issuer)
    and once for the filer. Merge them into one record per accession number."""
    filings = {}
    for e in entries:
        f = filings.setdefault(e["acc"], {"acc": e["acc"], "form": e["form"],
                                          "link": e["link"], "updated": e["updated"],
                                          "filers": [], "filer_ciks": []})
        if e["role"].lower().startswith("subject"):
            f["issuer"] = e["name"]
            f["issuer_cik"] = e["cik"]
            f["link"] = e["link"] or f["link"]
        else:
            if e["name"] not in f["filers"]:
                f["filers"].append(e["name"])
            if e["cik"] not in f["filer_ciks"]:
                f["filer_ciks"].append(e["cik"])
    # Only keep filings where we've seen the issuer entry; the rest get
    # picked up on a later run once the issuer entry shows up.
    return {k: v for k, v in filings.items() if "issuer_cik" in v}


def load_tickers():
    raw = json.loads(sec_get("https://www.sec.gov/files/company_tickers.json"))
    out = {}
    for row in raw.values():
        out.setdefault(str(row["cik_str"]).zfill(10), row["ticker"])
    return out


def cap_from_yahoo(ticker):
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.replace(".", "-"))
        fi = t.fast_info
        cap = fi.market_cap
        if cap and cap > 0:
            return float(cap)
        shares, price = fi.shares, fi.last_price
        if shares and price:
            return float(shares) * float(price)
    except Exception as e:  # noqa: BLE001
        print(f"  yahoo lookup failed for {ticker}: {e}")
    return None


def float_from_sec(cik):
    """Fallback: the public float the company reported on its last 10-K.
    Always a bit below market cap and can be up to a year old, but it's
    straight from the SEC and good enough to tell a microcap from a megacap."""
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/EntityPublicFloat.json"
    try:
        data = json.loads(sec_get(url))
        facts = data.get("units", {}).get("USD", [])
        if facts:
            latest = max(facts, key=lambda x: (x.get("end", ""), x.get("filed", "")))
            return float(latest["val"])
    except Exception:  # noqa: BLE001
        pass  # 404 = company doesn't report it (common for foreign issuers)
    return None


def filing_details(cik, acc):
    """Best-effort read of the structured 13D/13G XML for % owned and event date."""
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{acc.replace('-', '')}/primary_doc.xml")
    try:
        root = ET.fromstring(sec_get(url))
    except Exception:  # noqa: BLE001
        return {}
    pct, event, prev_13g = [], None, False
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        text = (el.text or "").strip()
        if not text:
            continue
        if tag == "previouslyfiledflag":
            prev_13g = text.lower() in ("true", "y", "yes", "1")
        elif "percent" in tag:
            try:
                pct.append(float(text.replace("%", "")))
            except ValueError:
                pass
        elif event is None and ("dateofevent" in tag or "eventdate" in tag):
            event = text
    out = {}
    if pct:
        out["pct"] = max(pct)
    if event:
        out["event_date"] = event
    if prev_13g:
        out["prev_13g_box"] = True
    return out


def recent_filings(cik):
    """Accession number -> (form, filing date) from EDGAR's submissions API.
    For an issuer this includes 13D/13G filings other people made about it."""
    try:
        data = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        r = data["filings"]["recent"]
        return {a: (f, d) for a, f, d in zip(r["accessionNumber"], r["form"], r["filingDate"])}
    except Exception:  # noqa: BLE001
        return {}


def had_prior_13g(issuer_cik, filer_ciks, acc, filed_on):
    """Backstop: did any of this filing's filers file a 13G on this issuer
    recently? Match accession numbers present in both the issuer's and the
    filer's EDGAR histories."""
    if not filer_ciks:
        return False
    since = (filed_on - timedelta(days=365 * CONVERSION_LOOKBACK_YEARS)).strftime("%Y-%m-%d")
    before = filed_on.strftime("%Y-%m-%d")
    issuer_13g = {a for a, (form, d) in recent_filings(issuer_cik).items()
                  if "13G" in form and since <= d <= before and a != acc}
    if not issuer_13g:
        return False
    for fc in filer_ciks:
        if issuer_13g & set(recent_filings(fc)):
            return True
    return False


# ----------------------------------------------------------------- output
def item_title(f):
    tick = f" ({f['ticker']})" if f.get("ticker") else ""
    prefix = "[Cap unknown] " if f.get("cap") is None else ""
    if f.get("converted"):
        prefix = "[13G→13D] " + prefix
    pct = f" · {f['pct']:g}%" if f.get("pct") is not None else ""
    filer = f" · by {f['filers'][0]}" if f.get("filers") else ""
    return f"{prefix}{f['form']} — {f['issuer']}{tick} · {fmt_cap(f.get('cap'))}{pct}{filer}"


def item_body(f):
    rows = [
        ("Form", f["form"]),
        ("Issuer", f"{f['issuer']} {('(' + f['ticker'] + ')') if f.get('ticker') else ''}"),
        ("Size", fmt_cap(f.get("cap")) + (" (public float from last 10-K)" if f.get("cap_src") == "float" else "")),
        ("Filed by", ", ".join(f.get("filers") or ["—"])),
        ("% of class", f"{f['pct']:g}%" if f.get("pct") is not None else "—"),
        ("Event date", f.get("event_date") or "—"),
        ("Converted from 13G", {"box": "Yes (cover-page box ticked)",
                                "history": "Yes (earlier 13G on EDGAR; box not ticked)",
                                "both": "Yes (box ticked and earlier 13G on EDGAR)"}
                               .get(f.get("converted"), "—")),
        ("Accession", f["acc"]),
    ]
    html = "".join(f"<tr><td><b>{escape(k)}</b></td><td>{escape(str(v))}</td></tr>" for k, v in rows)
    return f"<table>{html}</table><p><a href=\"{escape(f['link'])}\">Open filing on EDGAR</a></p>"


def to_rfc822(iso):
    try:
        return format_datetime(datetime.fromisoformat(iso))
    except Exception:  # noqa: BLE001
        return format_datetime(datetime.now(timezone.utc))


def write_rss(path, title, items):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             f"<title>{escape(title)}</title>",
             f"<link>{escape(FEED_BASE or 'https://www.sec.gov/')}</link>",
             f"<description>{escape(title)} under {fmt_cap(MAX_CAP)}</description>"]
    for f in items:
        parts.append(
            "<item>"
            f"<title>{escape(item_title(f))}</title>"
            f"<link>{escape(f['link'])}</link>"
            f"<guid isPermaLink=\"false\">{f['acc']}</guid>"
            f"<pubDate>{to_rfc822(f['updated'])}</pubDate>"
            f"<description>{escape(item_body(f))}</description>"
            "</item>")
    parts.append("</channel></rss>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_index(path, items):
    rows = []
    for f in items:
        conv = f.get("converted")
        rows.append(
            ("<tr class=conv>" if conv else "<tr>") +
            f"<td>{escape(f['updated'][:10])}</td>"
            f"<td>{escape(f['form'])}{' <b>13G→13D</b>' if conv else ''}</td>"
            f"<td><a href=\"{escape(f['link'])}\">{escape(f['issuer'])}</a></td>"
            f"<td>{escape(f.get('ticker') or '')}</td>"
            f"<td class=n>{escape(fmt_cap(f.get('cap')))}</td>"
            f"<td class=n>{(str(f['pct']) + '%') if f.get('pct') is not None else ''}</td>"
            f"<td>{escape(', '.join(f.get('filers') or []))}</td>"
            "</tr>")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Small-cap 13D / 13G</title>
<style>
body{{font:14px -apple-system,Segoe UI,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:6px 8px;border-bottom:1px solid #eee;text-align:left}}
th{{background:#f6f6f6}} .n{{text-align:right;white-space:nowrap}} .wrap{{overflow-x:auto}}
tr.conv td{{background:#fff4d6}}
</style></head><body>
<h2>Small-cap 13D / 13G filings (under {fmt_cap(MAX_CAP)})</h2>
<p>Updated {stamp} · RSS: <a href="13d.xml">13D</a> · <a href="13g.xml">13G</a> · highlighted rows = filer switched from 13G to 13D</p>
<div class="wrap"><table><tr><th>Filed</th><th>Form</th><th>Issuer</th><th>Ticker</th>
<th class=n>Size</th><th class=n>% class</th><th>Filed by</th></tr>
{''.join(rows)}</table></div></body></html>""", encoding="utf-8")


# ----------------------------------------------------------------- main
def main():
    if not USER_AGENT or "@" not in USER_AGENT:
        raise SystemExit("Set SEC_USER_AGENT to your name and email, e.g. 'Kyle Smith kyle@gmail.com'")

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    kept = state.get("kept", {})        # acc -> filing record shown in feeds
    seen = state.get("seen", {})        # acc -> date first evaluated
    if isinstance(seen, list):
        seen = {}
    caps = state.get("caps", {})        # cik -> {cap, src, checked}

    print("Loading ticker map...")
    tickers = load_tickers()
    now = datetime.now(timezone.utc)

    for short, form_type in FORMS.items():
        print(f"Fetching {form_type}...")
        filings = group_by_filing(fetch_current(form_type))
        new = [f for acc, f in filings.items() if acc not in seen]
        print(f"  {len(filings)} filings, {len(new)} new")

        for f in new:
            cik = f["issuer_cik"]
            f["ticker"] = tickers.get(cik)
            f["group"] = short

            cached = caps.get(cik)
            fresh = cached and (now - datetime.fromisoformat(cached["checked"])) < timedelta(days=CAP_CACHE_DAYS)
            if fresh:
                cap, src = cached["cap"], cached["src"]
            else:
                cap, src = (cap_from_yahoo(f["ticker"]), "yahoo") if f["ticker"] else (None, None)
                if cap is None:
                    cap, src = float_from_sec(cik), "float"
                    if cap is None:
                        src = None
                caps[cik] = {"cap": cap, "src": src, "checked": now.isoformat()}

            seen[f["acc"]] = now.isoformat()
            if cap is not None and cap > MAX_CAP:
                continue  # too big, skip

            f["cap"], f["cap_src"] = cap, src
            f.update(filing_details(cik, f["acc"]))

            # 13G -> 13D check, original 13Ds only (amendments repeat the box)
            if f["form"].upper() == "SCHEDULE 13D":
                try:
                    filed_on = datetime.fromisoformat(f["updated"])
                except Exception:  # noqa: BLE001
                    filed_on = now
                box = f.pop("prev_13g_box", False)
                hist = had_prior_13g(cik, f.get("filer_ciks"), f["acc"], filed_on)
                if box or hist:
                    f["converted"] = "both" if box and hist else ("box" if box else "history")
            else:
                f.pop("prev_13g_box", None)

            kept[f["acc"]] = f
            print(f"  + {item_title(f)}")

    # Drop old items so the feeds stay small
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    kept = {k: v for k, v in kept.items() if v["updated"] >= cutoff}
    seen = {a: d for a, d in seen.items() if d >= cutoff}

    items = sorted(kept.values(), key=lambda x: x["updated"], reverse=True)
    DOCS.mkdir(exist_ok=True)
    write_rss(DOCS / "13d.xml", "Small-cap 13D filings", [i for i in items if i["group"] == "13D"])
    write_rss(DOCS / "13g.xml", "Small-cap 13G filings", [i for i in items if i["group"] == "13G"])
    write_index(DOCS / "index.html", items)

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"kept": kept, "seen": seen, "caps": caps}, indent=1))
    print(f"Done. {len(items)} filings in feeds.")


if __name__ == "__main__":
    main()

