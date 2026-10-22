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
                    settlement, proxy fight, insider buying) and a "capital"
                    signal (tender, new buyback, special dividend, strategic
                    review, spin-off...) both landed within the last 12 months
    spinoff.xml     Form 10 registrations for spin-offs (with the parent and its
                    size) and 8-Ks where a small company announces a spin-off
    insider.xml     open-market buying by officers and directors (Form 4):
                    2+ insiders within 30 days, or one large purchase
    uplisting.xml   companies moving up from OTC or a foreign exchange to NYSE /
                    Nasdaq (8-A12B, Form 10 from OTC banks, 40FR12B, 20FR12B)
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

# Insider buying (Form 4). Purchases are open-market buys (code P) by an
# officer or director; buys tied to an offering or private placement don't count.
INSIDER_WINDOW_DAYS = 30    # look-back for a cluster
CLUSTER_MIN_INSIDERS = 2    # distinct officers/directors needed for a cluster
CLUSTER_MIN_USD = 100_000   # combined dollars needed for a cluster
MIN_BUY_USD = 10_000        # an insider's buys below this are ignored
BIG_BUY_USD = 250_000       # one insider buying this much can flag on its own, if it's also
BIG_BUY_CAP_PCT = 0.1       #   at least this % of market cap, or
BIG_BUY_ADD_PCT = 10        #   adds at least this % to their holdings (or is a new position)
PRIVATE_DEAL_CAP_PCT = 5    # a "purchase" over this % of market cap that doesn't say "open
                            # market" is a private deal or a data error; ignored
FORM4_PAGES = 12            # EDGAR's latest-filings feed holds ~1,200 Form 4 entries
FORM4_BUDGET = int(os.environ.get("FORM4_BUDGET") or 800)  # Form 4 reads per run
FORM4_BACKSTOP_DAYS = 4     # re-check this many days of EDGAR's daily index for missed Form 4s

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "state.json"
DOCS = ROOT / "docs"

FORMS = {"13D": "SCHEDULE 13D", "13G": "SCHEDULE 13G"}
# Event forms -> how many 100-filing pages to pull from EDGAR's latest-filings
# feed. EDGAR only keeps a few days of 8-Ks there (~600), so 6 pages is all.
EVENT_FORMS = {"8-K": 6, "SC TO-I": 2, "PREC14A": 1, "DEFC14A": 1, "DFAN14A": 3,
               "10-12B": 1, "10-12G": 1, "8-A12B": 2, "40FR12B": 1, "20FR12B": 1}
PROXY_FORMS = {"PREC14A", "DEFC14A", "DFAN14A"}
FORM10_FORMS = {"10-12B", "10-12G"}       # registration of a new company (spin-offs, OTC banks)
LISTING_FORMS = {"8-A12B", "40FR12B", "20FR12B"}  # registration on a U.S. exchange
SCAN_ITEMS = {"5.02", "1.01", "2.02", "7.01", "8.01"}  # 8-K items worth reading
FUND_SICS = {"", "6722", "6726"}  # investment companies (interval funds, BDCs)
SPAC_SIC = "6770"                 # blank checks
ATOM = "{http://www.w3.org/2005/Atom}"

# Signal tags and how they count toward the catalyst flag. Only "strong"
# signals qualify; weak ones show up in the timeline for context.
STRONG_CHANGE = {"13D", "13G→13D", "13D Item 4", "CEO change",
                 "Activist settlement", "Proxy fight", "Insider buying"}
WEAK_CHANGE = {"CFO change", "Officer change", "Board change", "Uplisting"}
STRONG_CAPITAL = {"Tender offer", "New buyback", "Buyback increase",
                  "Special dividend", "Dividend initiation", "Strategic review", "Spin-off"}
WEAK_CAPITAL = {"Capital allocation"}
LEADERSHIP_TAGS = {"CEO change", "CFO change", "Officer change", "Board change"}
CAPITAL_TAGS = (STRONG_CAPITAL | WEAK_CAPITAL) - {"Spin-off"}  # spin-offs get their own feed

FEEDS = {  # group -> (file, title)
    "13D": ("13d.xml", "Small-cap 13D filings"),
    "13G": ("13g.xml", "Small-cap 13G filings"),
    "LEAD": ("leadership.xml", "Small-cap CEO / board changes (8-K 5.02)"),
    "ACT": ("activism.xml", "Small-cap activist settlements and proxy fights"),
    "TENDER": ("tender.xml", "Small-cap issuer tender offers (SC TO-I)"),
    "CAP": ("capital.xml", "Small-cap capital allocation announcements"),
    "SPIN": ("spinoff.xml", "Spin-offs (Form 10 filings and small-cap announcements)"),
    "UPLIST": ("uplisting.xml", "Small-cap uplistings to NYSE / Nasdaq"),
    "INSIDER": ("insider.xml", "Small-cap insider buying (Form 4)"),
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


def fetch_current(form_type: str, pages: int = PAGES_PER_FORM, owner: str = "include"):
    """Return raw entries from EDGAR's 'latest filings' Atom feed. The type
    is a prefix match ("4" also returns 424B2s), so Form 4s use owner=only."""
    entries = []
    for page in range(pages):
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               f"&type={form_type.replace(' ', '+')}&company=&dateb=&owner={owner}"
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
    issuer. Form 4s use "Issuer" and "Reporting"."""
    filings = {}
    for e in entries:
        f = filings.setdefault(e["acc"], {"acc": e["acc"], "form": e["form"],
                                          "link": e["link"], "updated": e["updated"],
                                          "items": [], "filers": [], "filer_ciks": []})
        role = e["role"].lower()
        if role.startswith(("subject", "issuer")) or (role == "filer" and "issuer_cik" not in f):
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


_TITLES = {}  # cik -> company name from the SEC ticker file (for spin-off parents)


def load_tickers():
    raw = json.loads(sec_get("https://www.sec.gov/files/company_tickers.json"))
    out = {}
    for row in raw.values():
        cik = str(row["cik_str"]).zfill(10)
        out.setdefault(cik, row["ticker"])
        _TITLES.setdefault(cik, row.get("title") or "")
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
    # A small company announcing (or approving) a plan to spin something off.
    # Has its own negation list below, since announcements often say "would".
    ("Spin-off", r"(?:announc\w*|approv\w*|unanimously|(?:plans?|intends?|intention|decided|decision) to "
                 r"(?:pursue |explore |effect )?(?:a |the )?(?:separat|spin|split))[^.]{0,120}"
                 r"(?:spin-?off|split-?off|separat\w* into (?:two|three|separate|independent)|"
                 r"(?:separate|independent|stand-?alone),? publicly[- ]traded compan)", False),
]
EQUITY_RE = re.compile(r"\b(shares?|stock|equity)\b", re.I)
# Mentions of a spin-off that already happened, or a recap of one already announced
SPIN_NEGATION = re.compile(
    r"complet\w*|consummat\w*|following (?:the|our|its)|after the|subsequent to|prior to the|"
    r"previously announced|former parent|in connection with (?:the|our|its) (?:spin|separation)|"
    r"since (?:the|our|its)|record date|distribution date|when-issued|no assurance|forward-looking|"
    r"\bupdate|where to find|intends to file|proxy statement|Form 10\b", re.I)


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
            if tag == "Spin-off":
                if SPIN_NEGATION.search(s):
                    continue
            elif CAP_NEGATION.search(s) and not re.search(r"\bnew\b", s, re.I):
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


# ------------------------------------------- spin-offs and uplistings
# Registrants that are funds, ETFs or SPACs by name. (A brand-new spin-off
# often has no SIC code yet, so the SIC test in is_fund_or_spac can't be used.)
FUNDISH_NAME = re.compile(
    r"\bfund\b|\bETF\b|(?<!investment )\btrust\b|\bBDC\b|lending|credit (?:income|opportunit|partners)|"
    r"private (?:assets|equity|credit|capital)|income (?:fund|trust|opportunit)|portfolio|"
    r"acquisition corp|merger corp|growth corp|\bSPAC\b|\bL\.?P\.?$", re.I)
SPIN_RE = re.compile(r"pro rata distribution|separation and distribution agreement|"
                     r"(?:the|this|our|its) (?:proposed |planned )?(?:spin-?off|split-?off)\b", re.I)
OTC_QUOTED_RE = re.compile(
    r"(?:currently|is|are|been) (?:quoted|traded)[^.]{0,80}(?:OTCQX|OTCQB|OTC Pink|OTC Markets|"
    r"over[- ]the[- ]counter|OTC Bulletin)", re.I)
HOME_EXCHANGE_RE = re.compile(
    r"TSX Venture Exchange|Toronto Stock Exchange|Canadian Securities Exchange|Cboe Canada|NEO Exchange|"
    r"Australian Securities Exchange|\bASX\b|London Stock Exchange|(?-i:\bAIM\b)|Tel Aviv Stock Exchange|"
    r"Frankfurt Stock Exchange|Euronext|Oslo B[øo]rs|Nasdaq (?:Stockholm|Copenhagen|Helsinki)|"
    r"Hong Kong Stock Exchange|Tokyo Stock Exchange|\bJSE\b|OTCQX|OTCQB|OTC Pink", re.I)
US_EXCHANGE_RE = re.compile(r"NYSE American|NYSE Arca|New York Stock Exchange|\bNYSE\b|"
                            r"Nasdaq (?:Global Select|Global|Capital) Market|Nasdaq Stock Market|\bNasdaq\b", re.I)
TRANSFER_RE = re.compile(
    r"transfer\w*[^.]{0,100}(?:listing|from (?:the )?(?:NYSE|New York Stock Exchange|Nasdaq))|"
    r"currently listed on (?:the )?(?:NYSE|New York Stock Exchange|Nasdaq)|successor (?:issuer|registrant)|"
    r"Rule 12g-3|holding company reorganization|redomestic\w*|reincorporat\w*", re.I)
COMMON_CLASS_RE = re.compile(r"common (?:stock|shares)|ordinary shares|American Depositary|subordinate voting|"
                             r"class [ab] (?:common|shares|ordinary)|\bunits\b", re.I)
PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F", "6-K", "10-KT", "10-K405", "10-KSB", "10-QSB"}
# (Confidential drafts, DRS, don't count: foreign companies file them before a 20FR12B too.)
IPO_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B1", "1-A", "1-A/A", "S-11"}
RMT_RE = re.compile(r"Reverse Morris Trust", re.I)
SPIN_SNIP_RE = re.compile(r"pro rata distribution|distribution of (?:all|\d+%|at least|approximately)|"
                          r"tax-free (?:spin-?off|distribution)", re.I)


def main_and_exhibit_text(cik, acc, form, exhibit=True, limit=1_500_000):
    """Text of a filing's main document (and its EX-99.1, e.g. the spin-off
    information statement). Counts against the document budget."""
    global _doc_reads
    docs = filing_documents(cik, acc)
    _doc_reads += 1
    main = [u for t, u in docs if t.replace(" ", "").startswith(form[:5])][:1] or [u for _, u in docs][:1]
    texts = []
    for u in main + ([u for t, u in docs if t.startswith("EX-99")][:1] if exhibit else []):
        texts.append(html_to_text(sec_get(u)[:limit]))
        _doc_reads += 1
    return " ".join(texts)


def _norm_words(s):
    return re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("&", " and ")).split()


_STOP_PARENT = {"the", "our", "its", "company", "parent", "spinco", "distribution", "we", "us", "such",
                "remainco", "holders", "existing", "current", "record"}


def find_parent(text, own_cik, own_name):
    """The company doing the spin-off, matched to the SEC ticker file, e.g.
    "distribution ... to Corteva stockholders" -> Corteva, Inc. (CTVA)."""
    pats = [  # (pattern, weight): "spin-off by/from X" is the most direct statement
        (r"(?:spin-?off|split-?off|separation|distribution)[^.]{0,70}? (?:by|from) "
         r"([A-Z][A-Za-z0-9&.'’\- ]{1,60}?)(?=\s*\(|,| of | to | in |\.|’s|'s| and )", 2),
        (r"to (?:the )?(?:holders of )?(?:current |existing )?([A-Z][A-Za-z0-9&.'’\- ]{1,50}?)(?:’s|'s)? "
         r"(?:common |ordinary )?(?:stock|share)holders", 1),
        (r"holders of ([A-Z][A-Za-z0-9&.'’\- ]{1,50}?)(?:’s|'s)? (?:common stock|ordinary shares|shares)", 1),
    ]
    own = _norm_words(own_name)[:1]
    votes = {}
    for pat, weight in pats:
        for m in re.finditer(pat, text[:400_000]):
            words = _norm_words(m.group(1))
            if not words or words[0] in _STOP_PARENT or words[:1] == own or len("".join(words)) < 3:
                continue
            best = None
            for cik, title in _TITLES.items():
                tw = _norm_words(title)
                if tw[:len(words)] == words and cik != own_cik and (best is None or len(tw) < best[1]):
                    best = (cik, len(tw))
            if best:
                votes[best[0]] = votes.get(best[0], 0) + weight
    return max(votes, key=votes.get) if votes else None


def listing_history(cik, acc, filed):
    """What the company filed before this registration: periodic reports,
    an earlier exchange registration, an IPO prospectus, a Form 10."""
    try:
        r = submissions(cik)["filings"]["recent"]
    except Exception:  # noqa: BLE001
        return {}
    prior = [(a, f, d) for a, f, d in zip(r["accessionNumber"], r["form"], r["filingDate"])
             if a != acc and d <= filed]  # newest first
    file_no = next((n for f, n, d in zip(r["form"], r.get("fileNumber") or [""] * len(r["form"]), r["filingDate"])
                    if f in PERIODIC_FORMS and d <= filed), "")
    forms = {f for _, f, _ in prior}
    six_mo = (datetime.fromisoformat(filed) - timedelta(days=180)).strftime("%Y-%m-%d")
    year = (datetime.fromisoformat(filed) - timedelta(days=365)).strftime("%Y-%m-%d")
    annual = next(((a, f) for a, f, _ in prior if f in ("10-K", "20-F", "40-F")), None)
    return {
        "periodic": bool(forms & PERIODIC_FORMS),
        "listed": "8-A12B" in forms,  # already registered a class on an exchange
        "offering": next((a for a, f, d in prior if f in IPO_FORMS and d >= six_mo), None),
        # a SPAC merger or new holding company: merger registration, no annual report yet
        "merger": not annual and any(f in ("S-4", "S-4/A", "F-4", "F-4/A") and d >= year for _, f, d in prior),
        "form10": bool(forms & {"10-12B", "10-12B/A", "10-12G", "10-12G/A"}),
        "foreign_reg": any(f in ("40FR12B", "20FR12B") and d >= year for _, f, d in prior),
        "annual": annual,
        # SEC file numbers starting "001-" mean the stock is already registered
        # on an exchange; "000-" means it isn't (OTC or unlisted)
        "exchange_file_no": file_no.startswith("001-"),
    }


LISTED_ABROAD_RE = re.compile(
    r"(?:are|is) (?:currently |also )?(?:listed|traded|quoted)[^.]{0,60}(?:" + HOME_EXCHANGE_RE.pattern + ")", re.I)


def already_on_us_exchange(cik, annual):
    """Does the company's last 10-K / 20-F / 40-F cover page list a U.S.
    exchange under "Name of each exchange on which registered"? Catches
    companies listed since before EDGAR, whose original 8-A isn't online."""
    acc, form = annual
    text = main_and_exhibit_text(cik, acc, form, exhibit=False, limit=300_000)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", text))[:40_000]  # some covers hold escaped HTML
    for anchor in (r"Name of each exchange on which (?:each class is )?registered",
                   r"Section 12\(b\) of the (?:Securities Exchange )?Act"):
        m = re.search(anchor, text, re.I)
        if m and US_EXCHANGE_RE.search(text[m.end():m.end() + 500]):
            return True
    return False


def classify_registration(f, form, tickers, caps, now):
    """Decide whether a Form 10 / 8-A12B / 40FR12B / 20FR12B is a spin-off or
    an uplisting. Returns an event dict (tags, groups, detail...) or None."""
    cik, name, acc = f["issuer_cik"], f["issuer"], f["acc"]
    filed = (f.get("updated") or now.isoformat())[:10]
    if FUNDISH_NAME.search(name):
        return None
    subs = submissions(cik)
    if str(subs.get("sic") or "") in (FUND_SICS - {""}) | {SPAC_SIC} or subs.get("entityType") == "investment":
        return None

    if form in FORM10_FORMS:
        text = main_and_exhibit_text(cik, acc, form)
        spin = SPIN_RE.search(text)
        if spin:
            parent = find_parent(text, cik, name)
            sents = [s for s in split_sentences(text[:400_000])
                     if not re.search(r"\bExhibit\b", s) and len(s) <= 350]
            holders = re.compile(r"(?:stock|share)?holders", re.I)
            snip = (next((s for s in sents if SPIN_SNIP_RE.search(s) and holders.search(s)), None)
                    or next((s for s in sents if SPIN_SNIP_RE.search(s) or SPIN_RE.search(s)), spin.group(0)))
            kind = "Spin-off and merger (Reverse Morris Trust)" if RMT_RE.search(text[:400_000]) else "Form 10"
            ev = {"tags": ["Spin-off"], "groups": ["SPIN"], "kind": kind, "snips": {"Spin-off": snip[:400]}}
            if parent:
                pcap, psrc = get_cap(parent, tickers.get(parent), caps, now)
                ev.update(parent=_TITLES.get(parent) or parent, parent_cik=parent,
                          parent_ticker=tickers.get(parent), parent_cap=pcap)
            return ev
        otc = OTC_QUOTED_RE.search(text)
        if otc and form == "10-12B":  # an OTC company (often a bank) registering to list
            ex = US_EXCHANGE_RE.search(text[:20_000])
            snip = next((s for s in split_sentences(text[:600_000]) if OTC_QUOTED_RE.search(s)), otc.group(0))
            return {"tags": ["Uplisting"], "groups": ["UPLIST"], "kind": "Form 10 (OTC company registering)",
                    "to": ex.group(0) if ex else "", "snips": {"Uplisting": snip[:400]}}
        return None

    hist = listing_history(cik, acc, filed)
    if form == "8-A12B":
        if hist.get("listed") or hist.get("form10") or hist.get("merger") or hist.get("foreign_reg"):
            return None  # new class/transfer, a spin-off, a SPAC/merger, or already posted (40FR12B)
        home = None
        if not hist.get("periodic"):
            if hist.get("offering"):
                # An IPO, unless the prospectus says the stock already trades
                # OTC or on a foreign exchange (an "uplisting with offering")
                pro = main_and_exhibit_text(cik, hist["offering"], "S-1", exhibit=False, limit=800_000)
                home = OTC_QUOTED_RE.search(pro) or LISTED_ABROAD_RE.search(pro)
                if not home:
                    return None
                kind = "Uplisting with an offering"
            else:
                kind = "Direct listing (no IPO prospectus)"
        else:
            if hist.get("exchange_file_no") or (hist.get("annual") and already_on_us_exchange(cik, hist["annual"])):
                return None  # already exchange-listed; this is a transfer or a re-registration
            kind = "8-A12B" + (", with an offering in the last 6 months" if hist.get("offering") else "")
        text = main_and_exhibit_text(cik, acc, form, exhibit=False, limit=400_000)
        cover = text[:6000]
        if TRANSFER_RE.search(text[:60_000]) or not COMMON_CLASS_RE.search(cover):
            return None
        foreign_co = (str((subs.get("addresses") or {}).get("business", {}).get("isForeignLocation")) == "1"
                      and bool(subs.get("sic")))
        if kind.startswith("Direct") and not (foreign_co or HOME_EXCHANGE_RE.search(text) or OTC_QUOTED_RE.search(text)):
            return None  # no sign it trades anywhere today (often a fund or a new vehicle)
        ex = US_EXCHANGE_RE.search(cover)
        src = HOME_EXCHANGE_RE.search(home.group(0)) if home else HOME_EXCHANGE_RE.search(text)
        if not src and home:
            src = re.search(r"OTC\w*|over[- ]the[- ]counter", home.group(0), re.I)
        return {"tags": ["Uplisting"], "groups": ["UPLIST"], "kind": kind,
                "to": ex.group(0) if ex else "", "from": src.group(0) if src else ""}

    # 40FR12B / 20FR12B: a foreign company registering its shares for a U.S. listing
    if hist.get("offering") or hist.get("merger"):
        return None  # a foreign IPO or SPAC merger, not a company that already trades at home
    text = main_and_exhibit_text(cik, acc, form, exhibit=False, limit=600_000)
    ex = US_EXCHANGE_RE.search(text[:20_000])
    home = HOME_EXCHANGE_RE.search(text)
    return {"tags": ["Uplisting"], "groups": ["UPLIST"],
            "kind": "Foreign company, new U.S. listing (" + form + ")",
            "to": ex.group(0) if ex else "", "from": home.group(0) if home else ""}


# ----------------------------------------------------------- insider buying
OFFERING_RE = re.compile(
    r"public offering|registered direct|private placement|underwrit\w*|subscription agreement|"
    r"securities purchase agreement|purchase agreement with the (?:issuer|company)|at-the-market|"
    r"initial public offering|\bIPO\b|directly from the (?:issuer|company)", re.I)
# Venture funds buying in an IPO footnote the IPO on the conversion lines,
# not the purchase line, so any IPO mention in the filing disqualifies it
IPO_FILING_RE = re.compile(r"initial public offering|\bIPO\b", re.I)


def _num(x):
    try:
        return float(re.sub(r"[^\d.\-]", "", x or "") or 0)
    except ValueError:
        return 0.0


def parse_form4(raw: bytes):
    """Open-market purchases (code P) in a Form 4's XML. Returns None if the
    document can't be read."""
    m = re.search(rb"<ownershipDocument>.*?</ownershipDocument>", raw, re.S)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return None
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")

        def flag(tag, rel=rel):
            return rel is not None and (rel.findtext(tag) or "").strip().lower() in ("1", "true")
        title = (rel.findtext("officerTitle") if rel is not None else "") or ""
        owners.append({"name": (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip(),
                       "cik": (ro.findtext("reportingOwnerId/rptOwnerCik") or "").strip(),
                       "director": flag("isDirector"), "officer": flag("isOfficer"),
                       "ten": flag("isTenPercentOwner"), "title": title.strip()})
    notes = {fn.get("id"): " ".join(fn.itertext()) for fn in root.iter("footnote")}
    all_notes = " ".join(notes.values()) + " " + (root.findtext("remarks") or "")
    ipo = bool(IPO_FILING_RE.search(all_notes))
    buys, skipped = [], 0.0
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if (t.findtext("transactionCoding/transactionCode") or "").strip() != "P":
            continue
        if (t.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip() != "A":
            continue
        shares = _num(t.findtext("transactionAmounts/transactionShares/value"))
        price = _num(t.findtext("transactionAmounts/transactionPricePerShare/value"))
        if shares <= 0 or price <= 0:
            continue
        refs = " ".join(notes.get(fid.get("id"), "") for fid in t.iter("footnoteId"))
        if ipo or OFFERING_RE.search(refs):
            skipped += shares * price
            continue
        buys.append({"d": (t.findtext("transactionDate/value") or "")[:10], "shares": shares, "usd": shares * price,
                     "after": _num(t.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value"))})
    return {"issuer_cik": (root.findtext("issuer/issuerCik") or "").strip().zfill(10),
            "issuer": (root.findtext("issuer/issuerName") or "").strip(),
            "symbol": (root.findtext("issuer/issuerTradingSymbol") or "").strip(),
            "owners": owners, "buys": buys, "skipped_usd": skipped,
            "open_market": bool(re.search(r"open[- ]market", all_notes, re.I)),
            "plan": (root.findtext("aff10b5One") or "").strip().lower() in ("1", "true")}


def near_offering(cik, dates, days=3):
    """True if the company filed an offering prospectus (424B1/B4/B5) within a
    few days of these purchases: insiders buying into an IPO or follow-on."""
    try:
        r = submissions(cik)["filings"]["recent"]
    except Exception:  # noqa: BLE001
        return False
    offers = [datetime.fromisoformat(d) for f, d in zip(r["form"], r["filingDate"])
              if f in ("424B1", "424B4", "424B5")]
    for d in dates:
        try:
            bd = datetime.fromisoformat(d)
        except ValueError:
            continue
        if any(abs((bd - o).days) <= days for o in offers):
            return True
    return False


def owner_role(o):
    if o["officer"] and o["title"]:
        role = o["title"]
    elif o["officer"]:
        role = "Officer"
    elif o["director"]:
        role = "Director"
    elif o["ten"]:
        role = "10% owner"
    else:
        role = "Other"
    if o["director"] and o["officer"] and "director" not in role.lower():
        role += ", Director"
    return role


def form4_candidates(seen4, tickers, idx_done, now):
    """New Form 4s: EDGAR's latest-filings feed (newest ~2.5 days), plus the
    daily index for the last few days as a backstop on heavy filing days.
    Returns [(acc, issuer_cik or None, ciks, filed, index_day)]."""
    out, have = [], set()
    for f in group_by_filing(fetch_current("4", FORM4_PAGES, owner="only")).values():
        if f["form"] == "4" and f["acc"] not in seen4:
            out.append((f["acc"], f["issuer_cik"], [f["issuer_cik"]], f["updated"], None))
            have.add(f["acc"])
    for back in range(1, FORM4_BACKSTOP_DAYS + 1):
        day = (now - timedelta(days=back)).date()
        key = day.isoformat()
        if day.weekday() >= 5 or idx_done.get(key) == "done":
            continue
        q = (day.month - 1) // 3 + 1
        try:
            raw = sec_get(f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/"
                          f"form.{day:%Y%m%d}.idx").decode("latin-1")
        except Exception:  # noqa: BLE001
            continue  # not published yet (or a holiday); try next run
        rows = {}
        for line in raw.splitlines():
            if line.startswith("4 "):
                parts = line.split()
                acc = parts[-1].rsplit("/", 1)[-1].removesuffix(".txt")
                rows.setdefault(acc, set()).add(parts[-3].zfill(10))
        idx_done[key] = "pending"
        for acc, ciks in rows.items():
            if acc not in seen4 and acc not in have:
                out.append((acc, None, sorted(ciks), key + "T20:00:00-04:00", key))
                have.add(acc)
    return out


def process_form4(tickers, caps, seen4, buys, idx_done, now):
    """Read new Form 4s and record open-market purchases. Returns the set of
    issuer CIKs with new purchases."""
    touched, reads, deferred = set(), 0, set()
    stale = (now - timedelta(days=30)).isoformat()
    print("Fetching Form 4 (insider transactions)...")
    cands = form4_candidates(seen4, tickers, idx_done, now)
    print(f"  {len(cands)} new Form 4s")
    for acc, issuer, ciks, filed, day in cands:
        listed = [c for c in ciks if c in tickers]
        if not listed:
            seen4[acc] = now.isoformat()
            continue
        known = [caps[c] for c in listed if c in caps and caps[c].get("checked", "") >= stale]
        if known and len(known) == len(listed) and all(k["cap"] and k["cap"] > MAX_CAP for k in known):
            seen4[acc] = now.isoformat()  # issuer known to be too big; no need to read
            continue
        if reads >= FORM4_BUDGET:
            deferred.add(day)
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(listed[0])}/{acc.replace('-', '')}/{acc}.txt"
        try:
            doc = parse_form4(sec_get(url))
        except Exception as e:  # noqa: BLE001
            print(f"  couldn't read {acc}: {e}")
            deferred.add(day)
            continue
        reads += 1
        seen4[acc] = now.isoformat()
        if not doc or not doc["buys"] or not doc["owners"]:
            continue
        cik = doc["issuer_cik"]
        ticker = tickers.get(cik)
        if not ticker:
            continue
        cap, src = get_cap(cik, ticker, caps, now)
        if (cap is not None and cap > MAX_CAP) or is_fund_or_spac(cik, doc["issuer"]):
            continue
        if near_offering(cik, [b["d"] for b in doc["buys"]]):
            print(f"  - skipped {doc['issuer']} ({ticker}): bought within days of the company's own offering")
            continue
        o = doc["owners"][0]
        usd = sum(b["usd"] for b in doc["buys"])
        shares = sum(b["shares"] for b in doc["buys"])
        after = doc["buys"][-1]["after"]
        if cap and usd > cap * PRIVATE_DEAL_CAP_PCT / 100 and not doc["open_market"]:
            print(f"  - skipped {doc['issuer']} ({ticker}): {fmt_usd(usd)} is over "
                  f"{PRIVATE_DEAL_CAP_PCT}% of market cap with no open-market language")
            continue
        name = o["name"] + (f" + {len(doc['owners']) - 1} related" if len(doc["owners"]) > 1 else "")
        rec = {"acc": acc, "key": o["cik"] or o["name"], "owner": name, "role": owner_role(o),
               "insider": any(x["director"] or x["officer"] for x in doc["owners"]),
               "d": max(b["d"] for b in doc["buys"]) or filed[:10], "filed": filed,
               "usd": round(usd), "shares": shares, "price": round(usd / shares, 4),
               "pct_added": round(100 * shares / (after - shares), 1) if after > shares else None,
               "new_position": bool(after) and abs(after - shares) < 1,
               "plan": doc["plan"], "issuer": doc["issuer"], "ticker": ticker,
               "link": index_link(listed[0], acc)}
        lst = buys.setdefault(cik, [])
        if not any(b["acc"] == acc for b in lst):
            lst.append(rec)
            touched.add(cik)
            print(f"  $ {doc['issuer']} ({ticker}) · {rec['role']} {o['name']} bought {fmt_usd(usd)}")
    for key, v in list(idx_done.items()):
        if v == "pending" and key not in deferred:
            idx_done[key] = "done"
    print(f"  {reads} Form 4s read" + (" (budget used up; the rest next run)" if deferred else ""))
    return touched


def evaluate_buys(cik, buys, now, cap=None):
    """Officer/director buying in the last INSIDER_WINDOW_DAYS for one issuer.
    Returns (kind, qualifying insiders, everyone who bought)."""
    since = (now - timedelta(days=INSIDER_WINDOW_DAYS)).strftime("%Y-%m-%d")
    people = {}
    for b in sorted(buys.get(cik, []), key=lambda b: b["d"]):
        if b["d"] < since:
            continue
        p = people.setdefault(b["key"], {"owner": b["owner"], "role": b["role"], "insider": b["insider"],
                                         "usd": 0, "first": b["d"], "plan": False, "pct_added": None,
                                         "new_position": False, "link": b["link"], "n": 0})
        p["usd"] += b["usd"]
        p["last"], p["link"], p["n"] = b["d"], b["link"], p["n"] + 1
        p["plan"] = p["plan"] or b["plan"]
        p["new_position"] = p["new_position"] or b.get("new_position", False)
        if b.get("pct_added") is not None:
            p["pct_added"] = round((p["pct_added"] or 0) + b["pct_added"], 1)
    qual = sorted((p for p in people.values() if p["insider"] and p["usd"] >= MIN_BUY_USD),
                  key=lambda p: -p["usd"])

    def meaningful(p):  # big relative to the company or to the buyer's own stake
        return (p["usd"] >= BIG_BUY_USD and
                (cap is None or p["usd"] >= cap * BIG_BUY_CAP_PCT / 100 or p["new_position"]
                 or (p["pct_added"] or 0) >= BIG_BUY_ADD_PCT))
    kind = None
    if len(qual) >= CLUSTER_MIN_INSIDERS and sum(p["usd"] for p in qual) >= CLUSTER_MIN_USD:
        kind = "Cluster"
    elif any(meaningful(p) for p in qual):
        kind = "Large buy"
    return kind, qual, sorted(people.values(), key=lambda p: -p["usd"])


def build_insider(cik, buys, caps, old, now):
    kind, qual, everyone = evaluate_buys(cik, buys, now, (caps.get(cik) or {}).get("cap"))
    if not kind:
        return None
    latest = max(buys[cik], key=lambda b: b["d"])
    start = min(p["first"] for p in qual)
    prev = old.get(cik)
    if prev and prev.get("start", "") >= (now - timedelta(days=2 * INSIDER_WINDOW_DAYS)).strftime("%Y-%m-%d"):
        start = prev["start"]  # same buying episode; keep its id
    rec = {"cik": cik, "issuer": latest["issuer"], "ticker": latest["ticker"],
           "cap": (caps.get(cik) or {}).get("cap"), "kind": kind, "n": len(qual),
           "total": sum(p["usd"] for p in qual), "people": everyone, "start": start,
           "link": latest["link"]}
    if prev and prev.get("start") == start and rec["n"] <= prev.get("n", 0):
        rec["updated"] = prev["updated"]  # nothing new; don't re-surface it
        rec["n"] = prev["n"]
    else:
        rec["updated"] = now.isoformat()
    return rec


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
    if e.get("parent"):  # spin-off Form 10: the new company has no price yet
        ptick = f" ({e['parent_ticker']})" if e.get("parent_ticker") else ""
        return (f"[{', '.join(tags)}] {e['issuer']} · from {e['parent']}{ptick} · parent "
                f"{fmt_cap(e.get('parent_cap'))} · {e['form']}")
    if e.get("kind") and "SPIN" in e["groups"] and e["form"] in FORM10_FORMS:
        return f"[{', '.join(tags)}] {e['issuer']} · parent not identified · {e['form']}"
    if "UPLIST" in e.get("groups", []):
        route = " → ".join(x for x in (e.get("from"), e.get("to")) if x)
        return (f"[Uplisting] {e['issuer']}{tick} · {fmt_cap(e.get('cap'))} · {e['form']}"
                + (f" · {route}" if route else ""))
    return (f"[{', '.join(tags)}] {e['issuer']}{tick} · {fmt_cap(e.get('cap'))} · "
            f"{e['form']}{by}")


def event_body(e):
    rows = [("Form", e["form"] + (f" (Items {', '.join(e['items'])})" if e.get("items") else "")),
            ("Issuer", f"{e['issuer']} {('(' + e['ticker'] + ')') if e.get('ticker') else ''}"),
            ("Size", fmt_cap(e.get("cap")) + (" (public float from last 10-K)" if e.get("cap_src") == "float" else "")),
            ("Signals", ", ".join(e["tags"]))]
    if e.get("kind"):
        rows.append(("Type", e["kind"]))
    if e.get("parent"):
        rows.append(("Parent", f"{e['parent']}" + (f" ({e['parent_ticker']})" if e.get("parent_ticker") else "")
                     + f" · {fmt_cap(e.get('parent_cap'))}"))
    if e.get("from"):
        rows.append(("Trades now on", e["from"]))
    if e.get("to"):
        rows.append(("Listing on", e["to"]))
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


def insider_title(c):
    tick = f" ({c['ticker']})" if c.get("ticker") else ""
    who = ", ".join(p["role"] for p in c["people"] if p["insider"] and p["usd"] >= MIN_BUY_USD)
    label = f"Cluster: {c['n']} insiders" if c["kind"] == "Cluster" else "Large buy"
    return f"[{label}] {c['issuer']}{tick} · {fmt_cap(c.get('cap'))} · {fmt_usd(c['total'])} · {who}"


def insider_body(c):
    rows = "".join(
        f"<tr><td>{escape(p['owner'])}</td><td>{escape(p['role'])}</td><td class=n>{escape(fmt_usd(p['usd']))}</td>"
        f"<td>{escape(p['first'] if p['first'] == p['last'] else p['first'] + ' to ' + p['last'])}</td>"
        f"<td class=n>{escape('new position' if p.get('new_position') else str(p['pct_added']) + '%' if p.get('pct_added') is not None else '—')}</td>"
        f"<td>{'10b5-1 plan' if p.get('plan') else ''}{'' if p['insider'] else ' (doesn’t count: not an officer or director)'}</td>"
        f"<td><a href=\"{escape(p['link'])}\">Form 4</a></td></tr>" for p in c["people"])
    return (f"<p><b>Open-market buys in the last {INSIDER_WINDOW_DAYS} days</b> "
            f"(“added” = shares bought as a % of what they held before)</p>"
            f"<table><tr><th>Buyer</th><th>Role</th><th>Amount</th><th>Dates</th><th>Added</th><th></th><th></th></tr>"
            f"{rows}</table>")


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


def write_index(path, items, events, catalysts, insiders=()):
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
    ins_rows = "".join(
        f"<tr><td>{escape(c['updated'][:10])}</td><td>{escape(c.get('ticker') or '')}</td>"
        f"<td><a href=\"{escape(c['link'])}\">{escape(c['issuer'])}</a></td>"
        f"<td class=n>{escape(fmt_cap(c.get('cap')))}</td><td class=n>{escape(fmt_usd(c['total']))}</td>"
        f"<td>{escape('; '.join(p['role'] + ' ' + fmt_usd(p['usd']) for p in c['people'] if p['insider']))}</td>"
        "</tr>" for c in insiders)
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
<h3>Insider buying (officers and directors, last {INSIDER_WINDOW_DAYS} days)</h3>
<div class="wrap"><table><tr><th>Flagged</th><th>Ticker</th><th>Issuer</th><th class=n>Size</th><th class=n>Total</th><th>Buyers</th></tr>
{ins_rows or '<tr><td colspan=6>None yet</td></tr>'}</table></div>
<h3>Leadership, activism, tender, capital allocation, spin-off and uplisting filings</h3>
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
            if form != form_type:  # originals only; amendments are noise
                seen[acc] = now.isoformat()
                continue
            if form in FORM10_FORMS | LISTING_FORMS:
                # New registrations: a spin-off has no ticker yet, so these
                # skip the listed-issuer rule and are sized separately.
                if _doc_reads >= DOC_BUDGET:
                    continue  # not marked seen, picked up next run
                try:
                    reg = classify_registration(f, form, tickers, caps, now)
                except Exception as e:  # noqa: BLE001
                    print(f"  couldn't read {acc}: {e}")
                    continue  # retried next run
                seen[acc] = now.isoformat()
                if not reg:
                    continue
                # A foreign company's brand-new U.S. ticker often maps to the
                # wrong Yahoo quote, so no size is shown and no cutoff applied
                cap, src = (get_cap(cik, ticker, caps, now) if ticker and form not in ("40FR12B", "20FR12B")
                            else (None, None))
                if "UPLIST" in reg["groups"] and cap is not None and cap > MAX_CAP:
                    continue
                names[cik] = f["issuer"]
                e = {"acc": acc, "form": f["form"], "link": f["link"], "updated": f["updated"],
                     "issuer": f["issuer"], "issuer_cik": cik, "ticker": ticker, "cap": cap,
                     "cap_src": src, "items": [], "filers": [], "tags": reg["tags"],
                     "snips": reg.get("snips") or {}, "groups": reg["groups"]}
                for k in ("kind", "to", "from", "parent", "parent_cik", "parent_ticker", "parent_cap"):
                    if reg.get(k):
                        e[k] = reg[k]
                events[acc] = e
                if "Uplisting" in reg["tags"]:
                    add_signal(signals, cik, "Uplisting", f["updated"], acc, f["link"])
                pc = reg.get("parent_cik")
                if pc and (reg.get("parent_cap") is None or reg["parent_cap"] <= MAX_CAP):
                    add_signal(signals, pc, "Spin-off", f["updated"], acc, f["link"])
                    to_stack.add(pc)
                print(f"  + {event_title(e)}")
                continue
            if not ticker:  # listed issuers only
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
                if "Spin-off" in tags and not has_recent(signals, cik, "Spin-off", CAPITAL_REPEAT_DAYS, now, acc):
                    groups.append("SPIN")
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
    buys = state.get("buys", {})        # cik -> [open-market purchase records]
    seen4 = state.get("seen4", {})      # Form 4 acc -> date read (kept 10 days)
    f4_index = state.get("f4_index", {})  # daily-index date -> "pending"/"done"
    insiders = state.get("insiders", {})  # cik -> insider-buying feed record

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

    try:
        touched = process_form4(tickers, caps, seen4, buys, f4_index, now)
    except Exception as e:  # noqa: BLE001  (never let Form 4s break the other feeds)
        print(f"  Form 4 step failed, skipped this run: {e}")
        touched = set()
    for cik in touched | set(insiders):
        if cik not in buys:
            continue
        rec = build_insider(cik, buys, caps, insiders, now)
        if not rec:
            continue
        if rec["updated"] == now.isoformat():
            print(f"  ★ {insider_title(rec)}")
            latest = max(buys[cik], key=lambda b: b["d"])
            add_signal(signals, cik, "Insider buying", latest["d"], latest["acc"], latest["link"])
            names[cik] = rec["issuer"]
            to_stack.add(cik)
        insiders[cik] = rec

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
    buy_cutoff = cutoff[:10]
    buys = {c: [b for b in lst if b["d"] >= buy_cutoff] for c, lst in buys.items()}
    buys = {c: lst for c, lst in buys.items() if lst}
    insiders = {k: v for k, v in insiders.items() if v["updated"] >= cutoff}
    seen4_cutoff = (now - timedelta(days=10)).isoformat()
    seen4 = {a: d for a, d in seen4.items() if d >= seen4_cutoff}
    f4_index = {d: v for d, v in f4_index.items() if d >= seen4_cutoff[:10]}

    items = sorted(kept.values(), key=lambda x: x["updated"], reverse=True)
    evs = sorted(events.values(), key=lambda x: x["updated"], reverse=True)
    cats = sorted(catalysts.values(), key=lambda x: x["updated"], reverse=True)
    ins = sorted(insiders.values(), key=lambda x: x["updated"], reverse=True)
    DOCS.mkdir(exist_ok=True)
    for group, (fn, title) in FEEDS.items():
        if group in ("13D", "13G"):
            write_rss(DOCS / fn, title, [i for i in items if i["group"] == group])
        elif group == "INSIDER":
            write_rss(DOCS / fn, title, ins, title_fn=insider_title, body_fn=insider_body,
                      guid_fn=lambda c: f"{c['cik']}-buy-{c['start']}-{c['n']}")
        else:
            write_rss(DOCS / fn, title, [e for e in evs if group in e["groups"]],
                      title_fn=event_title, body_fn=event_body)
    write_rss(DOCS / "catalyst.xml", "Small-cap catalyst stacks", cats,
              title_fn=catalyst_title, body_fn=catalyst_body,
              guid_fn=lambda c: f"{c['cik']}-{c['n']}")
    write_index(DOCS / "index.html", items, evs, cats, ins)

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"kept": kept, "seen": seen, "caps": caps, "events": events,
                                      "signals": signals, "scanned": scanned,
                                      "catalysts": catalysts, "pending": pending,
                                      "buys": buys, "seen4": seen4, "f4_index": f4_index,
                                      "insiders": insiders}, indent=1))
    print(f"Done. {len(items)} 13D/G filings, {len(evs)} events, {len(cats)} catalyst stacks, "
          f"{len(ins)} insider-buying flags. {_doc_reads} documents read.")


if __name__ == "__main__":
    main()
