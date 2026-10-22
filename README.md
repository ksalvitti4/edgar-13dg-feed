# Small-cap 13D / 13G feed: setup guide

This builds RSS feeds for 13Ds, 13Gs, CEO and board changes, activist settlements and proxy fights, issuer tender offers, capital allocation announcements, and a "catalyst stack" flag, all limited to companies under your market-cap cutoff. It also builds a simple web page listing the same filings. Everything runs free on GitHub. It updates every 2 hours on weekdays, and you read the results in Inoreader.

Each item looks like this:

> **SCHEDULE 13D — Tiny Corp (TINY) · $42.1M · 7.4% · by Activist Fund LP**

That's the form, the issuer, the ticker, the size, the % of class held, and who filed. Clicking the item opens the filing on EDGAR.

Setup takes about 20 minutes, and you only do it once.

---

## Step 1: Create a GitHub account

Go to **github.com** and sign up with your Gmail. The free plan is all you need.

## Step 2: Create a repository

1. Click the **+** in the top right, then **New repository**.
2. Name it `edgar-13dg-feed`.
3. Set it to **Public**. GitHub's free web hosting only works for public repos. The only data in it is public SEC filings.
4. Check **Add a README file**, then click **Create repository**.

## Step 3: Upload the script files

1. In your new repo, click **Add file**, then **Upload files**.
2. Drag in `edgar_filter.py`, `requirements.txt`, and this `README.md` (it will replace the placeholder README).
3. Click **Commit changes**.

## Step 4: Add the scheduler file

Folders that start with a dot often don't come through drag-and-drop, so create this file by hand:

1. Click **Add file**, then **Create new file**.
2. In the file name box, type exactly: `.github/workflows/update.yml`
   (GitHub turns each `/` into a folder as you type.)
3. Open the `update.yml` file from the download, copy everything in it, and paste it into the editor.
4. Click **Commit changes**.

## Step 5: Add your SEC contact info

The SEC requires every automated request to identify who is making it.

1. In the repo, go to **Settings**, then **Secrets and variables**, then **Actions**.
2. On the **Secrets** tab, click **New repository secret**.
   - Name: `SEC_USER_AGENT`
   - Secret: your name and email, e.g. `Kyle Smith kylesmith@gmail.com`
3. Click **Add secret**.

## Step 6: Set your market-cap cutoff (optional)

The default is **$500M**. To change it:

1. Stay on the same page and switch to the **Variables** tab. Click **New repository variable**.
2. Name: `MAX_MARKET_CAP`
3. Value: the cutoff in plain dollars with no commas or $ sign. For example, `300000000` is $300M and `1000000000` is $1B.

You can change this value any time. New filings will use the new cutoff.

## Step 7: Run it for the first time

1. Click the **Actions** tab. If GitHub asks you to enable workflows, click the green button.
2. Click **Update 13D/G feeds** in the left list, then **Run workflow**, then the green **Run workflow** button.
3. Wait for the green check mark. The first run looks up a few hundred companies and can take 10–15 minutes. Later runs take a minute or two.

When it finishes, a `docs` folder and a `data` folder will appear in your repo.

## Step 8: Turn on the web page

1. Go to **Settings**, then **Pages** in the left menu.
2. Under **Build and deployment**, set Source to **Deploy from a branch**.
3. Set Branch to **main** and the folder to **/docs**, then click **Save**.
4. After a minute or two, the page shows your site address:
   `https://YOUR-USERNAME.github.io/edgar-13dg-feed/`

## Step 9: Add the filtered feeds to Inoreader

In Inoreader, click **Add new**, then **Feed**, and add each of these (replace YOUR-USERNAME):

- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/13d.xml`
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/13g.xml`

Rename them "13D – Small cap" and "13G – Small cap." You can keep the raw EDGAR feeds as a backup or unfollow them.

Add the catalyst feeds the same way:

- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/leadership.xml` — CEO, CFO, officer and board changes (8-K Item 5.02)
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/activism.xml` — cooperation/nomination agreements with investors, and proxy fights
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/tender.xml` — companies tendering for their own shares (SC TO-I)
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/capital.xml` — new or bigger buybacks, special dividends, dividend initiations, Dutch auctions, strategic reviews, capital allocation frameworks
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/catalyst.xml` — the stacking flag (see below). Start here if you only follow one.
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/insider.xml` — officers and directors buying stock on the open market (Form 4): 2+ insiders in 30 days, or one large purchase
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/spinoff.xml` — spin-off registrations (Form 10) showing the parent company and its size, plus small companies announcing a spin-off
- `https://YOUR-USERNAME.github.io/edgar-13dg-feed/uplisting.xml` — companies moving to NYSE or Nasdaq from OTC or a foreign exchange (TSX, TSXV, CSE, ASX and others)

The page at `https://YOUR-USERNAME.github.io/edgar-13dg-feed/` shows the same filings as a table, if you'd rather browse that way.

---

## How it works

- Every 2 hours on weekdays, the script pulls up to the last 500 13D filings and the last 500 13G filings from EDGAR. That's enough to avoid missing filings on busy 13G deadline days.
- It merges each filing's "Subject" and "Filed by" entries into one item, so you don't see duplicates.
- It finds the issuer's ticker using the SEC's own ticker file, then gets the market cap from Yahoo Finance.
- If Yahoo has no market cap, it falls back to the **public float** from the company's last 10-K. This number comes straight from the SEC, but it runs a bit below market cap and can be up to a year old. Those items say "(public float from last 10-K)."
- If neither source has a number, the filing is **kept** and marked **[Cap unknown]**. These are usually private companies, foreign issuers, SPACs, or brand-new listings. You get to decide whether they matter.
- For filings it keeps, it reads the filing's structured data to get the **% of class** and the **event date**.
- Filings stay in the feed for 45 days, then drop off.

### 13D Item 4 tags

13Ds and 13D/As get an **[Item 4: ...]** tag when the purpose section describes something the filer did or plans to do: `nominating` (nominations, board seats), `letter` (letters, presentations, settlement talks), `capital` (buybacks, special dividends, excess cash), or `sale` (strategic alternatives, sale of the company). The standard "may from time to time discuss (a) through (j)" boilerplate doesn't trigger a tag. These usually come months before a board change or a new capital allocation plan.

### Catalyst feeds

- **Leadership** reads every 8-K with Item 5.02 and tags it **CEO change**, **CFO change**, **Officer change** or **Board change**. Compensation-only 5.02s (bonuses, employment agreement amendments, severance terms) are dropped, and so are biographies of new hires that mention past jobs elsewhere.
- **Activism** includes 8-Ks describing a cooperation, nomination or standstill agreement with an investor, plus PREC14A, DEFC14A and DFAN14A filings. DFAN14As from operating companies (usually merger communications) are skipped, and a proxy fight is only posted once per 60 days per company.
- **Tender** is every original SC TO-I.
- **Capital** reads 8-Ks with Items 1.01, 2.02, 7.01 or 8.01 (including the press release exhibits) for new buyback authorizations, buyback increases, special dividends, dividend initiations, Dutch auctions/self-tenders, strategic reviews, and capital allocation frameworks. Sentences about existing or previously authorized programs, forward-looking boilerplate, and "strategic alternatives" that are really a search for financing are ignored. Each tag is posted once per 180 days per company, since earnings releases keep repeating "our ongoing review of strategic alternatives."
- These feeds only include companies with an exchange ticker and skip funds, BDCs and SPACs.
- Every item shows the sentence that triggered the tag, so you can triage without opening the filing.

### Insider buying

- Reads every new Form 4 (insider transaction report) for companies under the cutoff and keeps **open-market purchases** (transaction code P) by **officers and directors**. Sales, option exercises, grants and gifts are ignored.
- **Not counted:** purchases tied to an IPO, a public or registered direct offering, or a private placement (from the filing's footnotes, or a company offering prospectus filed within 3 days); "purchases" over 5% of the company's market cap that don't say "open market" (private block deals and data-entry errors); and funds, BDCs and SPACs. Buys by 10% holders who aren't officers or directors are listed in the item but don't trigger it.
- **Flags when, within 30 days:**
  - **Cluster:** 2 or more officers/directors each bought at least $10K, $100K combined, or
  - **Large buy:** one officer/director bought at least $250K, and that's at least 0.1% of the company's market cap or adds at least 10% to their holdings (or is a new position).
- Each item lists every buyer, their role, dollars, dates, how much they added to their stake, and whether it was under a 10b5-1 plan. The item re-appears if more insiders join the same buying episode.
- EDGAR's latest-filings list only holds about 2½ days of Form 4s, so each run also checks EDGAR's daily index for the last few days to pick up anything missed on heavy filing days. Up to 800 Form 4s are read per run (`FORM4_BUDGET`); the rest carry over.
- Insider buying counts as a **change** signal for the catalyst stack.

### Spin-offs

- **Form 10 (10-12B / 10-12G):** a new company registering its shares. Kept only when it describes a spin-off ("pro rata distribution," "separation and distribution agreement," "the spin-off"); the many private funds, BDCs and shells that also file 10-12Gs are dropped. The item names the **parent** and its market cap and quotes the distribution sentence. Spin-offs combined with a merger are labeled "Reverse Morris Trust."
- There's **no size cutoff** on Form 10 spin-offs, since a small spin-off from a large parent is the classic forced-selling setup, and the new company has no stock price yet.
- **8-K announcements:** the 8-Ks already read for the capital feed are also checked for a small company announcing or approving a spin-off or separation into independent companies. Updates, legal boilerplate and completed spin-offs are ignored, and each company is posted at most once per 180 days. A spin-off counts as a **capital** signal for the catalyst stack (for the parent, when it's under the cutoff).

### Uplistings

- **8-A12B** (registering stock on an exchange) is kept only when the company already files reports with the SEC but isn't exchange-listed yet: no earlier 8-A12B, an SEC file number starting "000-" rather than "001-", and its last annual report doesn't list a U.S. exchange. Exchange transfers, new share classes (preferred, notes, warrants), holding-company reorganizations, SPAC mergers and plain IPOs are dropped. An IPO is kept only if its prospectus says the stock already trades OTC or on a foreign exchange ("uplisting with an offering").
- **Form 10-12B from an OTC company:** OTC-quoted banks often register this way to list on Nasdaq.
- **40FR12B / 20FR12B:** a foreign company (40FR12B is Canada) registering its shares for a U.S. listing. These show "cap unknown," because a brand-new U.S. ticker often maps to the wrong Yahoo quote, and the size cutoff isn't applied.
- The item shows where the stock trades now and which exchange it's moving to, when the filing says.

### Catalyst stacking flag

The script keeps a 12-month history of signals for each company, from these feeds and from the company's EDGAR filing history. A company lands in `catalyst.xml` when it has both:

- a **change** signal: 13D, 13G→13D, 13D Item 4 tag, CEO change, activist settlement, proxy fight, or insider buying, and
- a **capital** signal: tender offer, new buyback, buyback increase, special dividend, dividend initiation, strategic review, or spin-off.

Board, CFO and officer changes, uplistings, and "capital allocation" language show up in the timeline but don't qualify on their own, since they're too common. Each item shows the signal timeline and whether the company is actually spending on buybacks (from XBRL cash-flow data vs. the prior year). A company re-appears in the feed whenever a new signal is added to its stack.

The script reads at most 900 filing documents per run (set with a `DOC_BUDGET` variable) plus up to 800 Form 4s (`FORM4_BUDGET`). Anything left over is picked up on the next run, so the first few runs after an upgrade take longer and fill in gradually.

## Troubleshooting

- **A red X in Actions:** click the failed run and open the **Build feeds** step to see the error. The most common cause is a missing or misspelled `SEC_USER_AGENT` secret, which must include an @ email.
- **"Permission denied" when saving results:** go to **Settings**, then **Actions**, then **General**, set **Workflow permissions** to **Read and write**, and save.
- **Lots of "yahoo lookup failed" lines in the log:** Yahoo sometimes throttles GitHub's servers. The script falls back to SEC public float automatically, so you'll mostly see more "public float" labels for a while. Nothing is lost.
- **The feeds stopped updating after a couple of months:** GitHub pauses scheduled jobs in quiet repos. Go to **Actions**, click the workflow, and click **Enable workflow** if you see that button.
- **You want different timing:** edit the `cron` line in `.github/workflows/update.yml`. The times are in UTC.
