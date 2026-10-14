"""
Small-cap EDGAR catalyst feeds.

Pulls the latest filings from EDGAR, looks up each issuer's market cap, keeps
only the ones under your cutoff, and writes these RSS feeds to docs/:

    13d.xml         SCHEDULE 13D filings (+ amendments)
    13g.xml         SCHEDULE 13G filings (+ amendments)
    leadership.xml  8-K Item 5.02: CEO / CFO / officer / board changes
                    (compensation-only 5.02s are dropped)
    activism.xml    cooperation / nomination / standstill agreements in 8-Ks,
                    plus proxy-fight filings (PREC14A, DEFC14A, DFAN14A)
    tender.xml      issuer tender offers (SC TO-I), funds excluded
    capital.xml     8-Ks announcing a new or bigger buyback, special dividend,
                    dividend initiation, Dutch auction, strategic review, or a
                    capital allocation framework
    catalyst.xml    issuers where a "change" signal (13D, CEO change, activist
                    settlement, proxy fight) and a "capital" signal (tender,
                    new buyback, special dividend, strategic review...) both
                    landed within the last 12 months
    index.html      a page listing everything

13D/13G filings whose market cap can't be found are KEPT and marked
"[Cap unknown]". The 8-K / tender / proxy feeds only include issuers with an
exchange ticker, since those are the ones you can actually buy.

Settings come from environment variables (set them in GitHub, see README):
    SEC_USER_AGENT   required by the SEC, e.g. "Kyle Smith kyle@gmail.com"
    MAX_MARKET_CAP   cutoff in dollars, default 500000000 ($500M)

New 13Ds from a filer that previously reported the same stake on a 13G are
tagged "[13G→13D]". Two checks, either one is enough:
    1. the filer ticked the "previously filed on Schedule 13G" box on the
       13D cover page (previouslyFiledFlag in the XML), or
    2. EDGAR shows a 13G from the same filer on the same issuer in the last
       CONVERSION_LOOKBACK_YEARS years (backstop for filers who skip the box).

13Ds (and 13D/As) are also tagged with "[Item 4: ...]" when the purpose
section describes an action already taken or planned, e.g. a nomination, a
letter to the board, or a push on buybacks or a sale. That is the earliest
point in the filing trail where a coming board or capital allocation change
usually shows up.
"""

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape, unescape
from pathlib import Path

# ----------------------------------------------------------------- settings
USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
MAX_CAP = float(os.environ.get("MAX_MARKET_CAP") or 500_000_000)
PAGES_PER_FORM = 5          # 5 pages x 100 = up to 500 recent filings per form
KEEP_DAYS = 45              # how long a filing stays in the feed
CAP_CACHE_DAYS = 5          # re-check a company's market cap after this many days
FEED_BASE = os.environ.get("FEED_BASE_URL", "")  # optional, for <link> tags
CONVERSION_LOOKBACK_YEARS = 3  # how far back to look for an earlier 13G

STACK_DAYS = 365            # window for the catalyst stacking flag
CAPITAL_REPEAT_DAYS = 180   # don't re-post the same capital tag for an issuer
PROXY_REPEAT_DAYS = 60      # don't re-post proxy-fight filings for an issuer
HISTORY_8K_SCANS = 12       # max older 8-Ks to read per issuer for stacking
DOC_BUDGET = int(os.environ.get("DOC_BUDGET") or 900)  # filing-document reads per run

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "state.json"
DOCS = ROOT / "docs"

FORMS = {"13D": "SCHEDULE 13D", "13G": "SCHEDULE 13G"}
# Event forms -> how many 100-filing pages to pull from EDGAR's latest-filings
# feed. EDGAR only keeps a few days of 8-Ks there (~600), so 6 pages is all.
EVENT_FORMS = {"8-K": 6, "SC TO-I": 2, "PREC14A": 1, "DEFC14A": 1, "DFAN14A": 3}
PROXY_FORMS = {"PREC14A", "DEFC14A", "DFAN14A"}
SCAN_ITEMS = {"5.02", "1.01", "2.02", "7.01", "8.01"}  # 8-K items worth reading
FUND_SICS = {"", "6722", "6726"}  # investment companies (interval funds, BDCs)
SPAC_SIC = "6770"                 # blank checks
ATOM = "{http://www.w3.org/2005/Atom}"

# Signal tags and how they count toward the catalyst flag. Only "strong"
# signals qualify; weak ones show up in the timeline for context.
STRONG_CHANGE = {"13D", "13G→13D", "13D Item 4", "CEO change",
                 "Activist settlement", "Proxy fight"}
WEAK_CHANGE = {"CFO change", "Officer change", "Board change"}
STRONG_CAPITAL = {"Tender offer", "New buyback", "Buyback increase",
                  "Special dividend", "Dividend initiation", "Strategic review"}
WEAK_CAPITAL = {"Capital allocation"}
LEADERSHIP_TAGS = {"CEO change", "CFO change", "Officer change", "Board change"}
CAPITAL_TAGS = STRONG_CAPITAL | WEAK_CAPITAL

FEEDS = {  # group -> (file, title)
    "13D": ("13d.xml", "Small-cap 13D filings"),
    "13G": ("13g.xml", "Small-cap 13G filings"),
    "LEAD": ("leadership.xml", "Small-cap CEO / board changes (8-K 5.02)"),
    "ACT": ("activism.xml", "Small-cap activist settlements and proxy fights"),
    "TENDER": ("tender.xml", "Small-cap issuer tender offers (SC TO-I)"),
    "CAP": ("capital.xml", "Small-cap capital allocation announcements"),
}

# ----------------------------------------------------------------- helpers
_last_sec_call = 0.0
_doc_reads = 0


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
            if getattr(e, "code", None) == 404 or attempt == 2:
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


def fmt_usd(v):
    if v == 0:
        return "$0"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v / 1e3:.0f}K"


def index_link(cik, acc):
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc.replace('-', '')}/{acc}-index.htm")


# Entry titles look like:
#   "SCHEDULE 13G/A - Creatd, Inc. (0001357671) (Subject)"
#   "8-K - Sadot Group Inc. (0001701756) (Filer)"
TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)\s*$")


def fetch_current(form_type: str, pages: int = PAGES_PER_FORM):
    """Return raw entries from EDGAR's 'latest filings' Atom feed."""
    entries = []
    for page in range(pages):
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
                "items": re.findall(r"Item (\d\.\d\d)", unescape(summary)),
            })
        if len(page_entries) < 100:
            break
    return entries


def group_by_filing(entries):
    """EDGAR lists each filing twice: once for the subject company (issuer)
    and once for the filer. Merge them into one record per accession number.
    Self-filed forms (8-K, SC TO-I) only have a "Filer" entry, which is the
    issuer."""
    filings = {}
    for e in entries:
        f = filings.setdefault(e["acc"], {"acc": e["acc"], "form": e["form"],
                                          "link": e["link"], "updated": e["updated"],
                                          "items": [], "filers": [], "filer_ciks": []})
        role = e["role"].lower()
        if role.startswith("subject") or (role == "filer" and "issuer_cik" not in f):
            f["issuer"] = e["name"]
            f["issuer_cik"] = e["cik"]
            f["link"] = e["link"] or f["link"]
            f["items"] = e.get("items") or f["items"]
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


def get_cap(cik, ticker, caps, now):
    """Market cap from cache, Yahoo, or SEC public float, in that order."""
    cached = caps.get(cik)
    if cached and (now - datetime.fromisoformat(cached["checked"])) < timedelta(days=CAP_CACHE_DAYS):
        return cached["cap"], cached["src"]
    cap, src = (cap_from_yahoo(ticker), "yahoo") if ticker else (None, None)
    if cap is None:
        cap, src = float_from_sec(cik), "float"
        if cap is None:
            src = None
    caps[cik] = {"cap": cap, "src": src, "checked": now.isoformat()}
    return cap, src


# ------------------------------------------------- 13D Item 4 intent tags
# A tag only fires on a sentence that describes something done or planned
# (sent a letter, nominated, intends to...), not the standard "may from time
# to time engage with management regarding (a) through (j)" boilerplate.
ACTION_RE = re.compile(
    r"\b(sent|deliver(?:ed|s)|submit(?:ted|s)|nominat(?:ed|es|ing|ion)|entered into|"
    r"intends? to|request(?:ed|s)|demand(?:ed|s)?|call(?:ed|s) (?:on|for)|urg(?:ed|es|ing)|"
    r"issued a (?:press release|public letter)|met with|propos(?:ed|es))\b", re.I)
BOILERPLATE_RE = re.compile(
    r"no (?:present )?plans? or proposals?|do(?:es)? not (?:currently |presently )?have any "
    r"(?:present )?plans?|\(a\) through \(j\)|subparagraphs? \(a\)|may,? from time to time|"
    r"joint filing agreement", re.I)
INTENT_TOPICS = [
    ("nominating", r"nominat\w*|board representation|board seats?|(?:appointment|election) of[^.]{0,40}director|"
                   r"reconstitut\w* the board|replace[^.]{0,40}(?:directors?|board|management|chief executive)"),
    ("letter", r"letter|cooperation agreement|settlement agreement|presentation"),
    ("capital", r"capital allocation|repurchase|buy-?back|return (?:of )?capital|special dividend|tender offer|"
                r"excess cash"),
    ("sale", r"strategic alternatives|sale of the (?:company|issuer)|merger|maximiz\w* (?:shareholder|stockholder) value|"
             r"undervalu\w*"),
]


def intent_tags(text):
    tags = []
    for sentence in split_sentences(text):
        if not ACTION_RE.search(sentence) or BOILERPLATE_RE.search(sentence):
            continue
        for name, pat in INTENT_TOPICS:
            if name not in tags and re.search(pat, sentence, re.I):
                tags.append(name)
    # A letter on its own (e.g. a lock-up letter) isn't interesting
    return tags if tags != ["letter"] else []


def filing_details(cik, acc):
    """Best-effort read of the structured 13D/13G XML for % owned, event date
    and the Item 4 purpose text."""
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{acc.replace('-', '')}/primary_doc.xml")
    try:
        root = ET.fromstring(sec_get(url))
    except Exception:  # noqa: BLE001
        return {}
    pct, event, prev_13g, purpose = [], None, False, ""
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        text = (el.text or "").strip()
        if not text:
            continue
        if tag == "previouslyfiledflag":
            prev_13g = text.lower() in ("true", "y", "yes", "1")
        elif tag == "transactionpurpose":
            purpose = text
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
    tags = intent_tags(purpose) if purpose else []
    if tags:
        out["intent"] = tags
    return out


_subs_cache = {}


def submissions(cik):
    """EDGAR's submissions API for a CIK (cached for the run)."""
    if cik not in _subs_cache:
        try:
            _subs_cache[cik] = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        except Exception:  # noqa: BLE001
            _subs_cache[cik] = {}
    return _subs_cache[cik]


def recent_filings(cik):
    """Accession number -> (form, filing date) from EDGAR's submissions API.
    For an issuer this includes 13D/13G filings other people made about it."""
    try:
        r = submissions(cik)["filings"]["recent"]
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


# ----------------------------------------------------------- 8-K text scan
def split_sentences(text):
    return [s for s in re.split(r"(?<=[.!?;])\s+(?=[A-Z(\"“])", text) if len(s) > 15]


def html_to_text(raw: bytes):
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def filing_documents(cik, acc):
    """[(type, url)] for the documents in a filing, from its index page."""
    page = sec_get(index_link(cik, acc)).decode("utf-8", "replace")
    docs = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        m = re.search(r'href="([^"]+)"', cells[2])
        if not m:
            continue
        href = m.group(1).replace("/ix?doc=", "")
        doc_type = re.sub(r"<[^>]+>", "", cells[3]).strip().upper()
        if href.lower().endswith((".htm", ".html", ".txt")):
            docs.append((doc_type, "https://www.sec.gov" + href))
    return docs


CHANGE_VERB = re.compile(
    r"\b(resign\w*|retire\w*|terminat\w*|step(?:s|ped|ping)? down|separat\w*|depart\w*|"
    r"not (?:to )?stand for re-?election|appoint\w*|named|hired?|succeed\w*|successor|"
    r"promot\w*|elected)\b", re.I)
CEO_RE = re.compile(r"chief executive officer|\bCEO\b|principal executive officer", re.I)
CFO_RE = re.compile(r"chief financial officer|\bCFO\b|principal financial officer", re.I)
OFFICER_RE = re.compile(r"\bpresident\b|chief \w+ officer|\bCOO\b|general counsel", re.I)
BOARD_RE = re.compile(
    r"as an? (?:independent |non-executive |class [iv]+ )?director|(?:to|from|of|on) the (?:company's )?"
    r"board(?: of directors)?|board seat|size of the board|member of the board|"
    r"chair(?:man|person|woman)? of the board", re.I)
COOP_RE = re.compile(r"cooperation agreement|nomination agreement|standstill", re.I)
# Commercial "cooperation agreements" (common with China-based issuers) and
# M&A standstills don't count; the sentence has to be about the board or vote.
COOP_CONTEXT = re.compile(r"\b(board|directors?|nominat\w*|appoint\w*|annual meeting|vot(?:e|ing))\b", re.I)
# Comp-only 5.02s describe what happens "upon termination" or to "named
# executive officers"; those words don't mean anyone is leaving.
NEO_RE = re.compile(r"named executive officers?", re.I)
CONDITIONAL_RE = re.compile(r"in the event|\bupon\b|\bif\b|would|entitled|severance|change (?:in|of) control|"
                            r"good reason|without cause|for cause", re.I)
# Biographies of new hires ("served as CFO of Acme until his resignation") and
# recaps of changes already filed don't describe a new change at this company.
SKIP_SENTENCE_RE = re.compile(
    r"\b(?:has |also |previously |most recently )?serv(?:ed|es) as\b|\bprior to (?:joining|that|this)|"
    r"previously (?:disclosed|reported)|\bfrom (?:19|20)\d\d (?:to|until|through)\b", re.I)
GAP_BLOCK_RE = re.compile(r"\bformer\b|director|officer|president|committee|\bboard\b", re.I)


def near_change(s, role_rx):
    """True if a change verb sits right next to this role, with no other role
    (or "former") in between: "appointed Jane Doe as CEO", "CFO, resigned"."""
    for m in role_rx.finditer(s):
        if re.search(r"former\W+(?:\w+\W+){0,3}$", s[max(0, m.start() - 40):m.start()], re.I):
            continue
        before = s[max(0, m.start() - 70):m.start()]
        verbs = list(CHANGE_VERB.finditer(before))
        if verbs and not GAP_BLOCK_RE.search(before[verbs[-1].end():]):
            return True
        after = s[m.end():m.end() + 50]
        v = CHANGE_VERB.search(after)
        if v and not GAP_BLOCK_RE.search(after[:v.start()]):
            return True
    return False

# Sentences in earnings releases and boilerplate that mention old programs
CAP_NEGATION = re.compile(
    r"previously (?:authorized|announced|approved|adopted)|existing|remain\w*|to date|"
    r"since (?:the )?inception|during the (?:quarter|period|year|three|six|nine|twelve|fiscal)|"
    r"no assurance|forward-looking|could|would|may not", re.I)
CAP_PATTERNS = [
    ("Tender offer", r"dutch auction|self[- ]tender|issuer tender offer|"
                     r"(?:commenc|launch|announc)\w*[^.]{0,60}tender offer to (?:purchase|repurchase|buy)", False),
    ("New buyback", r"(?:authoriz|approv|adopt)(?:ed|es)\b[^.]{0,100}(?:repurchase|buy-?back)", True),
    ("Buyback increase", r"(?:increas|expan|upsiz|replenish|extend)\w*[^.]{0,80}"
                         r"(?:repurchase|buy-?back) (?:program|plan|authori)", True),
    ("Special dividend", r"special (?:cash )?dividend", False),
    ("Dividend initiation", r"(?:initiat|inaugural|first[- ]ever|reinstat)\w*[^.]{0,60}dividend|"
                            r"declar\w* (?:its|the company's) first[^.]{0,30}dividend", False),
    ("Strategic review", r"(?:explor|review|evaluat|consider|pursu)\w*[^.]{0,60}strategic alternatives|"
                         r"strategic (?:review|alternatives) process", False),
    ("Capital allocation", r"capital allocation (?:framework|strategy|policy|priorities|review|committee)|"
                           r"return (?:of )?capital to (?:share|stock)holders", False),
]
EQUITY_RE = re.compile(r"\b(shares?|stock|equity)\b", re.I)


def item_502_text(text):
    """The Item 5.02 section(s) of an 8-K body."""
    text = re.split(r"Pursuant to the requirements of the Securities Exchange Act", text, flags=re.I)[0]
    parts = []
    for m in re.finditer(r"Item\s*5\.02", text, re.I):
        rest = text[m.end():m.end() + 8000]
        nxt = re.search(r"Item\s*(?!5\.02)\d\.\d\d", rest, re.I)
        parts.append(rest[:nxt.start()] if nxt else rest)
    return " ".join(p for p in parts if len(p) > 80) or text


def classify_leadership(text):
    tags, snips = [], {}
    for s in split_sentences(item_502_text(text)):
        check = NEO_RE.sub("", s)
        if CONDITIONAL_RE.search(check):
            check = re.sub(r"\b(terminat|separat|resign|depart|retire)\w*", "", check, flags=re.I)
        if not CHANGE_VERB.search(check) or SKIP_SENTENCE_RE.search(s):
            continue
        for tag, rx in (("CEO change", CEO_RE), ("CFO change", CFO_RE),
                        ("Board change", BOARD_RE), ("Officer change", OFFICER_RE)):
            if tag not in tags and near_change(check, rx):
                tags.append(tag)
                snips[tag] = s
    if "CEO change" in tags and re.search(r"\binterim\b", snips["CEO change"], re.I):
        snips["CEO change"] = "[Interim] " + snips["CEO change"]
    if "Officer change" in tags and ({"CEO change", "CFO change"} & set(tags)):
        tags.remove("Officer change")  # already covered by the specific tag
        snips.pop("Officer change", None)
    return tags, snips


def classify_capital(text):
    tags, snips = [], {}
    for s in split_sentences(text):
        for tag, pat, needs_equity in CAP_PATTERNS:
            if tag in tags or not re.search(pat, s, re.I):
                continue
            if needs_equity and not EQUITY_RE.search(s):
                continue
            if CAP_NEGATION.search(s) and not re.search(r"\bnew\b", s, re.I):
                continue
            if tag == "Strategic review" and re.search(
                    r"capital needs|financing|rais\w* (?:additional )?capital|liquidity", s, re.I):
                continue  # a company looking for money, not a strategic review
            tags.append(tag)
            snips[tag] = s
    if "Buyback increase" in tags and "New buyback" in tags:
        tags.remove("New buyback")
    return tags, snips


def scan_8k(cik, acc, items, scanned):
    """Read an 8-K (main document + press-release exhibits) and tag it.
    Results are cached in state so each filing is read once."""
    global _doc_reads
    if acc in scanned:
        return scanned[acc]
    result = {"tags": [], "snips": {}, "d": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    try:
        docs = filing_documents(cik, acc)
        _doc_reads += 1
        main = [u for t, u in docs if t.startswith("8-K")][:1]
        exhibits = [u for t, u in docs if t.startswith("EX-99")][:2]
        main_text = html_to_text(sec_get(main[0])[:3_000_000]) if main else ""
        _doc_reads += 1
        ex_text = ""
        if set(items) & {"1.01", "2.02", "7.01", "8.01"}:
            for u in exhibits:
                ex_text += " " + html_to_text(sec_get(u)[:3_000_000])
                _doc_reads += 1
    except Exception as e:  # noqa: BLE001
        print(f"  couldn't read {acc}: {e}")
        return result  # not cached, retried next time
    full = main_text + " " + ex_text
    if "5.02" in items:
        t, s = classify_leadership(main_text)
        result["tags"] += t
        result["snips"].update(s)
    if COOP_RE.search(full):
        m = next((s for s in split_sentences(full)
                  if COOP_RE.search(s) and COOP_CONTEXT.search(s) and INVESTOR_NAME.search(s)), "")
        if m:
            result["tags"].append("Activist settlement")
            result["snips"]["Activist settlement"] = m
    if set(items) & {"1.01", "2.02", "7.01", "8.01"}:
        t, s = classify_capital(full)
        result["tags"] += t
        result["snips"].update(s)
    result["snips"] = {k: v[:400] for k, v in result["snips"].items()}
    scanned[acc] = result
    return result


INVESTOR_NAME = re.compile(r"capital|partners|management|fund|advis[eo]rs|investors?|\bL\.?P\.?\b|LLC|"
                           r"holdings? (?:LP|LLC)|opportunit", re.I)


def is_fund_or_spac(cik, name):
    sic = str(submissions(cik).get("sic") or "")
    return (sic in FUND_SICS or sic == SPAC_SIC or
            bool(re.search(r"\bfund\b|\bBDC\b|private (?:assets|equity|credit)|acquisition corp", name, re.I)))


# ------------------------------------------------------- catalyst stacking
def add_signal(signals, cik, tag, date, acc, link):
    lst = signals.setdefault(cik, [])
    if not any(s["acc"] == acc and s["t"] == tag for s in lst):
        lst.append({"t": tag, "d": date[:10], "acc": acc, "link": link})


def has_recent(signals, cik, tag, days, now, exclude_acc=None):
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    return any(s["t"] == tag and s["d"] >= since and s["acc"] != exclude_acc
               for s in signals.get(cik, []))


def history_signals(cik, now, scanned):
    """Signals from the issuer's own EDGAR history for the stacking window,
    plus 8-Ks that haven't been read yet (candidates)."""
    since = (now - timedelta(days=STACK_DAYS)).strftime("%Y-%m-%d")
    try:
        r = submissions(cik)["filings"]["recent"]
    except Exception:  # noqa: BLE001
        return [], []
    found, candidates = [], []
    for acc, form, d, items in zip(r["accessionNumber"], r["form"], r["filingDate"], r["items"]):
        if d < since:
            continue
        if form in ("SCHEDULE 13D", "SC 13D"):
            found.append(("13D", d, acc))
        elif form == "SC TO-I":
            found.append(("Tender offer", d, acc))
        elif form in PROXY_FORMS:
            found.append(("Proxy fight", d, acc))
        elif form == "8-K":
            its = [i for i in (items or "").split(",") if i]
            if acc in scanned:
                found += [(t, d, acc) for t in scanned[acc]["tags"]]
            elif set(its) & SCAN_ITEMS:
                candidates.append((acc, d, its))
    return found, candidates


def evaluate_stack(cik, now, signals, scanned):
    """Merge history into the issuer's signals. Returns True when finished,
    False if it ran out of document budget (it'll be retried next run)."""
    found, candidates = history_signals(cik, now, scanned)
    for tag, d, acc in found:
        add_signal(signals, cik, tag, d, acc, index_link(cik, acc))

    def state():
        tags = {s["t"] for s in window(signals.get(cik, []), now)}
        return bool(tags & STRONG_CHANGE), bool(tags & STRONG_CAPITAL)

    change, capital = state()
    if change == capital:  # both (done) or neither (nothing to stack on)
        return True
    for acc, d, its in candidates[:HISTORY_8K_SCANS]:
        if _doc_reads >= DOC_BUDGET:
            return False
        if change and not (set(its) & {"1.01", "2.02", "7.01", "8.01"}):
            continue  # looking for capital signals
        if capital and not (set(its) & {"5.02", "1.01", "8.01"}):
            continue  # looking for change signals
        for tag in scan_8k(cik, acc, its, scanned)["tags"]:
            add_signal(signals, cik, tag, d, acc, index_link(cik, acc))
        change, capital = state()
        if change and capital:
            break
    return True


def window(sigs, now):
    since = (now - timedelta(days=STACK_DAYS)).strftime("%Y-%m-%d")
    return [s for s in sigs if s["d"] >= since]


def buyback_spend(cik):
    """Is the company actually buying back stock? Latest reported cash spent
    on repurchases (year-to-date) vs. the same period a year earlier."""
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/"
           "PaymentsForRepurchaseOfCommonStock.json")
    try:
        facts = json.loads(sec_get(url)).get("units", {}).get("USD", [])
    except Exception:  # noqa: BLE001
        return "no repurchase spending reported in XBRL"
    def days(f):
        return (datetime.fromisoformat(f["end"]) - datetime.fromisoformat(f["start"])).days
    facts = [f for f in facts if f.get("form") in ("10-Q", "10-K") and f.get("start") and days(f) <= 400]
    if not facts:
        return "no repurchase spending reported in XBRL"
    latest = max(facts, key=lambda f: (f["end"], f.get("filed", "")))
    if latest["end"] < (datetime.now(timezone.utc) - timedelta(days=550)).strftime("%Y-%m-%d"):
        return "no recent repurchase spending reported in XBRL"
    span = (datetime.fromisoformat(latest["end"]) - datetime.fromisoformat(latest["start"])).days
    prior = [f for f in facts
             if abs((datetime.fromisoformat(latest["end"]) - datetime.fromisoformat(f["end"])).days - 365) < 20
             and abs((datetime.fromisoformat(f["end"]) - datetime.fromisoformat(f["start"])).days - span) < 20]
    months = round(span / 30.4)
    out = f"{fmt_usd(latest['val'])} spent on buybacks in the {months} months to {latest['end']}"
    if prior:
        out += f" (prior year: {fmt_usd(prior[0]['val'])})"
    return out


def build_catalyst(cik, now, signals, names, tickers, caps, old):
    sigs = sorted(window(signals.get(cik, []), now), key=lambda s: (s["d"], s["t"]))
    tags = {s["t"] for s in sigs}
    if not (tags & STRONG_CHANGE and tags & STRONG_CAPITAL):
        return None
    name = names.get(cik) or submissions(cik).get("name") or cik
    if is_fund_or_spac(cik, name):
        return None
    rec = {
        "cik": cik,
        "issuer": name,
        "ticker": tickers.get(cik),
        "cap": (caps.get(cik) or {}).get("cap"),
        "timeline": sigs,
        "n": len(tags),  # distinct signal types; re-posts when a new type lands
        "updated": max(s["d"] for s in sigs) + "T00:00:00+00:00",
    }
    prev = old.get(cik)
    if prev and rec["n"] <= prev.get("n", 0):  # nothing new (or signals aged out)
        rec["buyback"] = prev.get("buyback")  # nothing new, keep old item
        rec["updated"] = prev["updated"]
    else:
        rec["buyback"] = buyback_spend(cik)
        rec["updated"] = now.isoformat()  # surfaces as new in the reader
    return rec


# ----------------------------------------------------------------- output
def item_title(f):
    tick = f" ({f['ticker']})" if f.get("ticker") else ""
    prefix = "[Cap unknown] " if f.get("cap") is None else ""
    if f.get("intent"):
        prefix = f"[Item 4: {', '.join(f['intent'])}] " + prefix
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
        ("Item 4 intent", ", ".join(f.get("intent") or ["—"])),
        ("Accession", f["acc"]),
    ]
    html = "".join(f"<tr><td><b>{escape(k)}</b></td><td>{escape(str(v))}</td></tr>" for k, v in rows)
    return f"<table>{html}</table><p><a href=\"{escape(f['link'])}\">Open filing on EDGAR</a></p>"


def event_title(e):
    tick = f" ({e['ticker']})" if e.get("ticker") else ""
    by = f" · by {e['filers'][0]}" if e.get("filers") and e["form"] in PROXY_FORMS else ""
    interim = (e.get("snips") or {}).get("CEO change", "").startswith("[Interim]")
    tags = [t + (" (interim)" if t == "CEO change" and interim else "") for t in e["tags"]]
    return (f"[{', '.join(tags)}] {e['issuer']}{tick} · {fmt_cap(e.get('cap'))} · "
            f"{e['form']}{by}")


def event_body(e):
    rows = [("Form", e["form"] + (f" (Items {', '.join(e['items'])})" if e.get("items") else "")),
            ("Issuer", f"{e['issuer']} {('(' + e['ticker'] + ')') if e.get('ticker') else ''}"),
            ("Size", fmt_cap(e.get("cap")) + (" (public float from last 10-K)" if e.get("cap_src") == "float" else "")),
            ("Signals", ", ".join(e["tags"]))]
    if e.get("filers"):
        rows.append(("Filed by", ", ".join(e["filers"])))
    html = "".join(f"<tr><td><b>{escape(k)}</b></td><td>{escape(str(v))}</td></tr>" for k, v in rows)
    quotes = "".join(f"<p><b>{escape(t)}:</b> “{escape(s)}”</p>" for t, s in (e.get("snips") or {}).items())
    return (f"<table>{html}</table>{quotes}"
            f"<p><a href=\"{escape(e['link'])}\">Open filing on EDGAR</a></p>")


def compact(timeline):
    """Collapse repeat filings of the same signal: one row per type with
    first/last date and count, in order of first appearance."""
    rows = {}
    for s in sorted(timeline, key=lambda s: s["d"]):
        r = rows.setdefault(s["t"], {"t": s["t"], "first": s["d"], "accs": set()})
        r["last"], r["link"] = s["d"], s["link"]
        r["accs"].add(s["acc"])
    out = []
    for r in rows.values():
        n = len(r["accs"])
        dates = r["first"] if r["first"] == r["last"] else f"{r['first']} to {r['last']}"
        out.append({"t": r["t"], "dates": dates, "label": r["t"] + (f" ({n} filings)" if n > 1 else ""),
                    "link": r["link"]})
    return out


def catalyst_title(c):
    tick = f" ({c['ticker']})" if c.get("ticker") else ""
    path = " → ".join(dict.fromkeys(s["t"] for s in c["timeline"]))
    return f"[Stack {c['n']}] {c['issuer']}{tick} · {fmt_cap(c.get('cap'))} · {path}"


def catalyst_body(c):
    rows = "".join(
        f"<tr><td>{escape(r['dates'])}</td><td>{escape(r['label'])}</td>"
        f"<td><a href=\"{escape(r['link'])}\">latest filing</a></td></tr>" for r in compact(c["timeline"]))
    return (f"<p><b>Buyback execution:</b> {escape(c.get('buyback') or '—')}</p>"
            f"<table><tr><th>Date</th><th>Signal</th><th></th></tr>{rows}</table>")


def to_rfc822(iso):
    try:
        return format_datetime(datetime.fromisoformat(iso))
    except Exception:  # noqa: BLE001
        return format_datetime(datetime.now(timezone.utc))


def write_rss(path, title, items, title_fn=item_title, body_fn=item_body, guid_fn=lambda f: f["acc"]):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             f"<title>{escape(title)}</title>",
             f"<link>{escape(FEED_BASE or 'https://www.sec.gov/')}</link>",
             f"<description>{escape(title)} under {fmt_cap(MAX_CAP)}</description>"]
    for f in items:
        link = f.get("link") or (f["timeline"][-1]["link"] if f.get("timeline") else "")
        parts.append(
            "<item>"
            f"<title>{escape(title_fn(f))}</title>"
            f"<link>{escape(link)}</link>"
            f"<guid isPermaLink=\"false\">{escape(guid_fn(f))}</guid>"
            f"<pubDate>{to_rfc822(f['updated'])}</pubDate>"
            f"<description>{escape(body_fn(f))}</description>"
            "</item>")
    parts.append("</channel></rss>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_index(path, items, events, catalysts):
    rows = []
    for f in items:
        conv = f.get("converted")
        rows.append(
            ("<tr class=conv>" if conv else "<tr>") +
            f"<td>{escape(f['updated'][:10])}</td>"
            f"<td>{escape(f['form'])}{' <b>13G→13D</b>' if conv else ''}"
            f"{(' <i>Item 4: ' + escape(', '.join(f['intent'])) + '</i>') if f.get('intent') else ''}</td>"
            f"<td><a href=\"{escape(f['link'])}\">{escape(f['issuer'])}</a></td>"
            f"<td>{escape(f.get('ticker') or '')}</td>"
            f"<td class=n>{escape(fmt_cap(f.get('cap')))}</td>"
            f"<td class=n>{(str(f['pct']) + '%') if f.get('pct') is not None else ''}</td>"
            f"<td>{escape(', '.join(f.get('filers') or []))}</td>"
            "</tr>")
    cat_rows = "".join(
        f"<tr><td>{escape(c['ticker'] or '')}</td><td>{escape(c['issuer'])}</td>"
        f"<td class=n>{escape(fmt_cap(c.get('cap')))}</td>"
        f"<td>{' → '.join(escape(r['dates'] + ' ' + r['label']) for r in compact(c['timeline']))}</td>"
        f"<td>{escape(c.get('buyback') or '')}</td></tr>" for c in catalysts)
    ev_rows = "".join(
        f"<tr><td>{escape(e['updated'][:10])}</td><td>{escape(', '.join(e['tags']))}</td>"
        f"<td><a href=\"{escape(e['link'])}\">{escape(e['issuer'])}</a></td>"
        f"<td>{escape(e.get('ticker') or '')}</td><td class=n>{escape(fmt_cap(e.get('cap')))}</td>"
        f"<td>{escape(e['form'])}</td></tr>" for e in events)
    links = " · ".join(f'<a href="{fn}">{escape(t.replace("Small-cap ", ""))}</a>'
                       for fn, t in [*FEEDS.values(), ("catalyst.xml", "Catalyst stacks")])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Small-cap EDGAR catalysts</title>
<style>
body{{font:14px -apple-system,Segoe UI,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{background:#f6f6f6}} .n{{text-align:right;white-space:nowrap}} .wrap{{overflow-x:auto}}
tr.conv td{{background:#fff4d6}} h3{{margin-top:32px}}
</style></head><body>
<h2>Small-cap EDGAR catalysts (under {fmt_cap(MAX_CAP)})</h2>
<p>Updated {stamp} · RSS: {links}</p>
<h3>Catalyst stacks (change + capital signal within 12 months)</h3>
<div class="wrap"><table><tr><th>Ticker</th><th>Issuer</th><th class=n>Size</th><th>Timeline</th><th>Buyback execution</th></tr>
{cat_rows or '<tr><td colspan=5>None yet</td></tr>'}</table></div>
<h3>Leadership, activism, tender and capital allocation filings</h3>
<div class="wrap"><table><tr><th>Filed</th><th>Signals</th><th>Issuer</th><th>Ticker</th><th class=n>Size</th><th>Form</th></tr>
{ev_rows or '<tr><td colspan=6>None yet</td></tr>'}</table></div>
<h3>13D / 13G filings</h3>
<p>Highlighted rows = filer switched from 13G to 13D</p>
<div class="wrap"><table><tr><th>Filed</th><th>Form</th><th>Issuer</th><th>Ticker</th>
<th class=n>Size</th><th class=n>% class</th><th>Filed by</th></tr>
{''.join(rows)}</table></div></body></html>""", encoding="utf-8")


# ----------------------------------------------------------------- main
def process_13dg(tickers, kept, seen, caps, signals, to_stack, now):
    for short, form_type in FORMS.items():
        print(f"Fetching {form_type}...")
        filings = group_by_filing(fetch_current(form_type))
        new = [f for acc, f in filings.items() if acc not in seen]
        print(f"  {len(filings)} filings, {len(new)} new")

        for f in new:
            cik = f["issuer_cik"]
            f["ticker"] = tickers.get(cik)
            f["group"] = short
            f.pop("items", None)
            cap, src = get_cap(cik, f["ticker"], caps, now)

            seen[f["acc"]] = now.isoformat()
            if cap is not None and cap > MAX_CAP:
                continue  # too big, skip

            f["cap"], f["cap_src"] = cap, src
            f.update(filing_details(cik, f["acc"]))
            if short != "13D":
                f.pop("intent", None)  # 13Gs are passive by definition

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

            if short == "13D":
                tag = None
                if f.get("converted"):
                    tag = "13G→13D"
                elif f["form"].upper() == "SCHEDULE 13D":
                    tag = "13D"
                if f.get("intent"):
                    add_signal(signals, cik, "13D Item 4", f["updated"], f["acc"], f["link"])
                if tag:
                    add_signal(signals, cik, tag, f["updated"], f["acc"], f["link"])
                if tag or f.get("intent"):
                    to_stack.add(cik)


def process_events(tickers, events, seen, caps, signals, scanned, to_stack, names, now):
    for form_type, pages in EVENT_FORMS.items():
        print(f"Fetching {form_type}...")
        filings = group_by_filing(fetch_current(form_type, pages))
        new = [f for acc, f in filings.items() if acc not in seen]
        print(f"  {len(filings)} filings, {len(new)} new")

        budget_note = False
        for f in new:
            acc, cik, form = f["acc"], f["issuer_cik"], f["form"].upper()
            ticker = tickers.get(cik)
            # Originals only (amendments are noise); listed issuers only
            if form != form_type or not ticker:
                seen[acc] = now.isoformat()
                continue
            if form == "8-K" and not set(f["items"]) & SCAN_ITEMS:
                seen[acc] = now.isoformat()
                continue
            if form == "8-K" and acc not in scanned and _doc_reads >= DOC_BUDGET:
                if not budget_note:
                    print("  document budget used up; the rest will be read next run")
                    budget_note = True
                continue  # not marked seen, picked up next run
            cap, src = get_cap(cik, ticker, caps, now)
            if (cap is not None and cap > MAX_CAP) or is_fund_or_spac(cik, f["issuer"]):
                seen[acc] = now.isoformat()
                continue

            tags, snips, groups = [], {}, []
            if form == "8-K":
                res = scan_8k(cik, acc, f["items"], scanned)
                if acc not in scanned:
                    continue  # read failed; not marked seen, retried next run
                tags, snips = list(res["tags"]), dict(res["snips"])
                if set(tags) & LEADERSHIP_TAGS:
                    groups.append("LEAD")
                if "Activist settlement" in tags:
                    groups.append("ACT")
                fresh_cap = [t for t in tags if t in CAPITAL_TAGS
                             and not has_recent(signals, cik, t, CAPITAL_REPEAT_DAYS, now, acc)]
                if fresh_cap:
                    groups.append("CAP")
            elif form == "SC TO-I":
                tags, groups = ["Tender offer"], ["TENDER"]
            elif form in PROXY_FORMS:
                activist = form != "DFAN14A" or any(INVESTOR_NAME.search(n) for n in f["filers"])
                if activist:
                    tags = ["Proxy fight"]
                    if not has_recent(signals, cik, "Proxy fight", PROXY_REPEAT_DAYS, now, acc):
                        groups = ["ACT"]
            seen[acc] = now.isoformat()

            for t in tags:
                add_signal(signals, cik, t, f["updated"], acc, f["link"])
            if set(tags) & (STRONG_CHANGE | STRONG_CAPITAL):
                to_stack.add(cik)
            if not groups:
                continue
            names[cik] = f["issuer"]
            e = {"acc": acc, "form": f["form"], "link": f["link"], "updated": f["updated"],
                 "issuer": f["issuer"], "issuer_cik": cik, "ticker": ticker, "cap": cap,
                 "cap_src": src, "items": f["items"], "filers": f["filers"], "tags": tags,
                 "snips": snips, "groups": groups}
            events[acc] = e
            print(f"  + {event_title(e)}")


def main():
    if not USER_AGENT or "@" not in USER_AGENT:
        raise SystemExit("Set SEC_USER_AGENT to your name and email, e.g. 'Kyle Smith kyle@gmail.com'")

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    kept = state.get("kept", {})        # acc -> 13D/13G record shown in feeds
    seen = state.get("seen", {})        # acc -> date first evaluated
    if isinstance(seen, list):
        seen = {}
    caps = state.get("caps", {})        # cik -> {cap, src, checked}
    events = state.get("events", {})    # acc -> 8-K / tender / proxy record
    first_run = "signals" not in state
    signals = state.get("signals", {})  # cik -> [{t, d, acc, link}]
    scanned = state.get("scanned", {})  # acc -> 8-K tags (cache)
    catalysts = state.get("catalysts", {})  # cik -> stack record
    to_stack = set(state.get("pending", []))
    names = {f["issuer_cik"]: f["issuer"] for f in [*kept.values(), *events.values()]}

    print("Loading ticker map...")
    tickers = load_tickers()
    now = datetime.now(timezone.utc)

    if first_run:  # seed the stacking flag from 13Ds already in the feed
        for f in kept.values():
            if f["group"] == "13D" and f["form"].upper() == "SCHEDULE 13D":
                add_signal(signals, f["issuer_cik"], "13G→13D" if f.get("converted") else "13D",
                           f["updated"], f["acc"], f["link"])
                to_stack.add(f["issuer_cik"])

    process_13dg(tickers, kept, seen, caps, signals, to_stack, now)
    process_events(tickers, events, seen, caps, signals, scanned, to_stack, names, now)

    print(f"Checking {len(to_stack)} issuers for catalyst stacks...")
    pending = []
    for cik in sorted(to_stack):
        if _doc_reads >= DOC_BUDGET or not evaluate_stack(cik, now, signals, scanned):
            pending.append(cik)
            continue
        rec = build_catalyst(cik, now, signals, names, tickers, caps, catalysts)
        if rec:
            if catalysts.get(cik, {}).get("n") != rec["n"]:
                print(f"  ★ {catalyst_title(rec)}")
            catalysts[cik] = rec
    if pending:
        print(f"  {len(pending)} issuers left for next run (document budget)")

    # Drop old items so the feeds and state stay small
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    kept = {k: v for k, v in kept.items() if v["updated"] >= cutoff}
    events = {k: v for k, v in events.items() if v["updated"] >= cutoff}
    catalysts = {k: v for k, v in catalysts.items() if v["updated"] >= cutoff}
    seen = {a: d for a, d in seen.items() if d >= cutoff}
    sig_cutoff = (now - timedelta(days=STACK_DAYS + 30)).strftime("%Y-%m-%d")
    signals = {c: [s for s in lst if s["d"] >= sig_cutoff] for c, lst in signals.items()}
    signals = {c: lst for c, lst in signals.items() if lst}
    scanned = {a: v for a, v in scanned.items() if v["d"] >= sig_cutoff}

    items = sorted(kept.values(), key=lambda x: x["updated"], reverse=True)
    evs = sorted(events.values(), key=lambda x: x["updated"], reverse=True)
    cats = sorted(catalysts.values(), key=lambda x: x["updated"], reverse=True)
    DOCS.mkdir(exist_ok=True)
    for group, (fn, title) in FEEDS.items():
        if group in ("13D", "13G"):
            write_rss(DOCS / fn, title, [i for i in items if i["group"] == group])
        else:
            write_rss(DOCS / fn, title, [e for e in evs if group in e["groups"]],
                      title_fn=event_title, body_fn=event_body)
    write_rss(DOCS / "catalyst.xml", "Small-cap catalyst stacks", cats,
              title_fn=catalyst_title, body_fn=catalyst_body,
              guid_fn=lambda c: f"{c['cik']}-{c['n']}")
    write_index(DOCS / "index.html", items, evs, cats)

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"kept": kept, "seen": seen, "caps": caps, "events": events,
                                      "signals": signals, "scanned": scanned,
                                      "catalysts": catalysts, "pending": pending}, indent=1))
    print(f"Done. {len(items)} 13D/G filings, {len(evs)} events, {len(cats)} catalyst stacks. "
          f"{_doc_reads} documents read.")


if __name__ == "__main__":
    main()
