# URL Consistency Check Pipeline — Project Knowledge Base

## Quick Start (paste this in a new Cowork chat)

```
I'm working on the URL Consistency Check pipeline in the folder I've selected.
Please read CLAUDE.md first — it has the full project knowledge base, domain
rules, analyst feedback, and architecture. Then ask me what I need.
```

---

## Project Overview

Automated classification engine for URL consistency failures in MSCI's physical
asset / issuer data. Given a URL flagged as inconsistent with an issuer, the
pipeline decides whether the URL is **valid** (belongs to the issuer or its
subsidiaries) or needs **manual review**.

**Owner:** Yash Singh (yash.singh@msci.com), DC – Assets Data team.

---

## Architecture — 3-Layer Pipeline

```
Layer 1 — Deterministic Rules (instant, 100% precision)
  ├── Generic URL check (Google Maps, LinkedIn, etc.) → URLC02
  ├── Third-party registry check (registries, aggregators) → URLC02
  ├── Report host / regulatory portal check → URLC01
  ├── Child table match (parent stem → URLC01, child stem → Manual Review)
  ├── Company URL column match (stem + subdomain) → URLC01
  ├── Subsidiary flag + company domain guard → URLC01
  └── Upstream suggestion (URL_CONSISTENCY_SUGGESTED_ISSUER_ID) → Manual Review

Layer 2 — Majority Domain (heuristic, name-verified only)
  └── If domain stem is the most common for an issuer AND matches issuer name → URLC01
      Otherwise → LLM review

Layer 3 — LLM / Cache Verification (for remaining ambiguous combos)
  └── Keyed by (ISSUER_ID, domain_stem) → URLC01 or URLC02
      Cache: cache/llm_verdicts.json (12,390+ entries)
      Reasons: cache/llm_reasons.json
```

---

## Verdict Modes (`--verdict-mode`)

The pipeline supports five output modes, selectable via `--verdict-mode v1|v2|v3|v4|v5`:

### v1 (default) — Conservative
| Verdict | Meaning |
|---------|---------|
| URLC01 | Valid — URL belongs to issuer |
| Manual Review | Needs analyst review (includes all URLC02 + unclear) |

### v2 — Granular (Trust Dictionary, Verify LLM)

| Verdict | Meaning |
|---------|---------|
| URLC01 | Valid — URL belongs to issuer (dictionary-confirmed OR LLM-detected company-owned) |
| URLC02 | Third-party domain — dictionary-confirmed (generic, registry, aggregator) |
| URLC03 | Subsidiary found in child_issuers table — replace with correct issuer ID |
| Manual Review | LLM-detected third-party — not yet in dictionary, needs analyst review |

#### v2 verdict flow (3 stages)

**Stage 1 — Deterministic rules (dictionary-based, trusted):**
- `AUTO_URLC01_*` decisions (report host, parent URL, company URL, subsidiary,
  majority domain) → **URLC01** — these are from the hardcoded domain lists
- `AUTO_URLC02_*` decisions (generic, registry) → **URLC02**
- `MANUAL_REVIEW_CHILD_TABLE` → **URLC03**
- `MANUAL_REVIEW_SUBSIDIARY` → **URLC01** (subsidiary flag set but not in child table)

**Stage 2 — LLM verdicts start as Manual Review:**
- `LLM_URLC01` and `LLM_MAJ_URLC01` → **Manual Review** (not in dictionary, needs review)
- `LLM_URLC02` → **URLC02** (LLM confirmed third-party — stays third-party)

**Stage 3 — Post-processing: promote company-owned LLM rows back to URLC01:**
After explanations are generated, the pipeline checks LLM Manual Review rows.
If the explanation indicates company ownership or financial filing, the row is
promoted to URLC01. Patterns that trigger promotion:

| Pattern in explanation | Example |
|----------------------|---------|
| `(company-owned)` | "Storage subdomain (company-owned)", "CDN subdomain (company-owned)" |
| `own domain` | "Domino's Pizza Enterprises own domain" |
| `own info site` | "Bakery Info - Greggs own info site" |
| `parent domain` / `parent (` | "KBFG - Kookmin Bank parent (KB Financial Group)" |
| `subsidiary` | "Iberchem - Croda International subsidiary" |
| `Domain stem matches issuer name` | "Domain stem matches issuer name (SHAKE SHACK INC.)" |
| `Majority domain verified by LLM` | "Majority domain verified by LLM: ITO EN global site" |
| `likely company-owned` | "Short subdomain pattern 'tsmc' (likely company-owned)" |
| `subdomain` (at end) | "PSE - Philip Morris CR subdomain" |

Patterns that stay Manual Review (third-party override):
- `news page` at end — e.g. "KDH News - Dongjin Semichem news page"
- `Company-specific subdomain/page (xxx)` — third-party sites like irbank, costar, cbinsights

#### v2 key distinction: URLC02 vs Manual Review

| | URLC02 | Manual Review |
|--|--------|---------------|
| **Source** | Hardcoded dictionary | LLM classification |
| **Confidence** | Analyst-validated, 100% certain | LLM says third-party, not yet validated |
| **Examples** | LinkedIn, Wikipedia, Google Maps, opencorporates, glassdoor | irbank, minedocs, costar, cbinsights, digitimes |
| **Action** | No review needed | Analyst reviews; confirmed domains can be added to dictionary |

#### v2 special rules

- If `Is_Subsidiary=1` but the subsidiary is NOT found in the child_issuers table
  → verdict is **URLC01** with explanation: *"Subsidiary detected (upstream flag) —
  not part of child issuer table; URL treated as valid for parent issuer"*
- Company URL enrichment should come from the **child issuer table**
  (`child_df.groupby('issuer_id')['url'].first()`) rather than the input's
  `COMPANY_DOMAIN` column, which may be unreliable

### v3 — Flag==1 mode
Same as v1 (URLC01 + Manual Review), semantically for `URL_CONSISTENCY_FLAG==1` rows.

### v4 — Strict Company-Only + Tag Column

| Verdict | Meaning |
|---------|---------|
| URLC01 | URL directly belongs to the company itself (not subsidiaries) |
| URLC02 | ALL third-party URLs including stock exchanges, gov registries, aggregators |
| Manual_Review | URL belongs to a subsidiary (both in child table and not) |

#### v4 key differences from v1–v3

1. **Report hosts / regulatory portals → URLC02** (not URLC01). Remarks show
   "Part of allowed regulatory/exchange list — {portal name}" so analysts know
   the domain is on the allowed list but still classified as third-party.
2. **ALL subsidiaries → Manual_Review** — regardless of whether the subsidiary is
   in the child issuer table or only flagged by upstream. No subsidiary URL is
   auto-approved as URLC01.
3. **LLM-detected subsidiaries demoted** — if an LLM explanation mentions
   "subsidiary", "parent domain", or "parent company", the row is demoted from
   URLC01 to Manual_Review in post-processing.

#### v4 Tag column

Every flagged row gets a `tag` column describing the corporate relationship
between the URL's owner entity and the issuer being checked:

| Tag | Description |
|-----|-------------|
| `real_subsidiary_with_issuer_id` | Confirmed subsidiary, leaf has a known issuer_id in child table |
| `real_subsidiary_without_issuer_id` | Confirmed subsidiary, but leaf not in our SEC database |
| `ultimate_parent` | Listed entity is the ultimate parent of the issuer (issuer is itself a child) |
| `parent` | Listed entity is an ancestor (not the ultimate parent) |
| `affiliate_not_subsidiary` | Same corporate family, but not in each other's lineage |
| `unrelated_entity` | Completely different corporate family (third-party) |

Tag derivation logic:
- `MANUAL_REVIEW_CHILD_TABLE` + suggested issuer ID → `real_subsidiary_with_issuer_id`
- `MANUAL_REVIEW_CHILD_TABLE` without issuer ID → `real_subsidiary_without_issuer_id`
- `MANUAL_REVIEW_SUBSIDIARY` or `AUTO_URLC01_SUBSIDIARY` → `real_subsidiary_without_issuer_id`
- If issuer is a child in the child table and URL matches parent's domain → `ultimate_parent` or `parent`
- If URL matches a sibling entity under the same parent → `affiliate_not_subsidiary`
- All third-party decisions → `unrelated_entity`
- Direct company match with no special relationship → `None` (tag left empty)

#### v4 remarks format

| Verdict | Remark prefix |
|---------|--------------|
| URLC01 | `Company-owned: {reason}` |
| URLC02 (allowed list) | `Third-party: Part of allowed regulatory/exchange list — {portal name}` |
| URLC02 (not allowed) | `Third-party: {reason} — not on allowed list` |
| Manual_Review (child table) | `Subsidiary URL — found in child issuer table as {ID} ({name})` |
| Manual_Review (no child) | `Subsidiary URL — upstream flag set but NOT found in child issuer table` |

### v5 — Like v4 + Allowlisted Portals → URLC03 + Tag Column

| Verdict | Meaning |
|---------|---------|
| URLC01 | URL directly belongs to the company itself (not subsidiaries) |
| URLC02 | Third-party URLs NOT on the regulatory/exchange/government allowlist |
| URLC03 | Allowlisted third-party: regulatory/exchange/government portal (e.g. SEC EDGAR, TASE, NASDAQ) |
| Manual_Review | URL belongs to a subsidiary (both in child table and not) |

#### v5 key differences from v4

1. **Allowlisted report hosts / regulatory portals → URLC03** (not URLC02). v4 lumps all
   third-party into URLC02; v5 splits out the allowlisted portals (stock exchanges,
   regulatory filing systems, IR hosting CDNs) into URLC03 so analysts can distinguish
   "known-good third-party" from "unknown third-party".
2. **LLM-detected URLs on allowlisted portals → URLC03** — if an LLM-classified URL is
   on a domain in REPORT_HOST_DOMAINS, it gets URLC03 instead of URLC02.
3. **Upstream DOMAIN_MISMATCH post-processing** — URLC01 rows where the upstream system
   flagged DOMAIN_MISMATCH and the URL is NOT on the company's own domain are demoted:
   allowlisted portals → URLC03, others → URLC02.
4. **Tag column** — same 6 tags as v4 (`real_subsidiary_with_issuer_id`,
   `real_subsidiary_without_issuer_id`, `ultimate_parent`, `parent`,
   `affiliate_not_subsidiary`, `unrelated_entity`).

#### v5 remarks format

| Verdict | Remark prefix |
|---------|--------------|
| URLC01 | `Company-owned: {reason}` |
| URLC02 (not allowlisted) | `Third-party: {reason} — not on allowlist` |
| URLC03 (allowlisted) | `Allowed third-party (URLC03): regulatory/exchange/government portal — {portal name}` |
| Manual_Review (child table) | `Subsidiary URL — found in child issuer table as {ID} ({name})` |
| Manual_Review (no child) | `Subsidiary URL — upstream flag set but NOT found in child issuer table` |

#### v5 vs v4 decision comparison

| Decision | v4 Verdict | v5 Verdict |
|----------|-----------|-----------|
| `AUTO_URLC01_REPORT_HOST` | URLC02 | URLC03 |
| `AUTO_URLC02_GENERIC` | URLC02 | URLC02 (or URLC03 if on allowlist) |
| `AUTO_URLC02_REGISTRY` | URLC02 | URLC02 (or URLC03 if on allowlist) |
| `LLM_URLC01` (company-owned) | URLC01 | URLC01 |
| `LLM_URLC01` (allowlisted portal) | URLC02 | URLC03 |
| `LLM_URLC01` (other third-party) | URLC02 | URLC02 |
| `LLM_URLC02` (allowlisted portal) | URLC02 | URLC03 |
| `LLM_URLC02` (other third-party) | URLC02 | URLC02 |
| Subsidiary decisions | Manual_Review | Manual_Review |

---

## File Structure

```
URL_CONSITENCY_CHECK/
├── CLAUDE.md                  ← THIS FILE — project knowledge base
├── .env / .env.example        ← API keys (Anthropic, OpenAI, Azure)
├── requirements.txt
├── README.md
│
├── scripts/
│   ├── url_consistency_engine.py   ← Core engine (rules, LLM prompts, pipeline)
│   ├── pipeline_common.py          ← Shared utilities (cache, explanations, Excel formatting)
│   ├── run_pipeline.py             ← Unified entry point (--mode full|flagged, --verdict-mode v1|v2|v3|v4|v5)
│   ├── run_full_check.py           ← Standalone: classify ALL rows
│   ├── run_flagged_check.py        ← Standalone: classify URL_CONSISTENCY_CHECK==1 only
│   └── run_cowork_check.py         ← Cowork mode: Claude-in-session as the LLM
│
├── input/
│   ├── issuer_child_flagged.xlsx   ← Child/subsidiary table (REQUIRED)
│   └── <data files>.xlsx|.pkl|.csv
│
├── output/                         ← Results written here
│
└── cache/
    ├── llm_verdicts.json           ← LLM verdict cache (12,812+ entries)
    └── llm_reasons.json            ← LLM reason cache
```

---

## Running the Pipeline

```bash
# Flagged check (default) — only URL_CONSISTENCY_CHECK==1 rows
python scripts/run_pipeline.py --input input/data.xlsx

# Full check — ALL rows
python scripts/run_pipeline.py --mode full --input input/data.xlsx

# With company URL column (enables subdomain matching)
python scripts/run_pipeline.py --input input/data.xlsx --company-url-col COMPANY_URL

# With verdict mode
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2

# With live LLM verification (API)
python scripts/run_pipeline.py --input input/data.xlsx --provider anthropic

# Cowork mode (Claude in-session classifies unresolved combos)
python scripts/run_pipeline.py --input input/data.xlsx --provider cowork
```

**In Cowork** — the typical workflow is:
1. User uploads/places data file in `input/`
2. Claude runs the pipeline via Bash
3. If unresolved combos remain, Claude classifies them using its own knowledge
4. Claude writes verdicts to cache and re-runs for final output

---

## Domain Classification Rules — Critical Knowledge

### REPORT_HOST_DOMAINS → URLC01 (regulatory portals & IR hosting)

These are official filing portals where issuers upload their own documents.
A URL on these domains belongs to the issuer → always URLC01.

| Region | Domains | Portal Name |
|--------|---------|-------------|
| China | cninfo.com.cn, sse.com.cn | CNINFO / Shanghai Stock Exchange |
| Japan | edinet-fsa.go.jp, jpx.co.jp | EDINET / Japan Exchange Group |
| Korea | dart.fss.or.kr, kind.krx.co.kr | DART / KIND (Korea Exchange disclosure) |
| Hong Kong | hkexnews.hk | HKEX filings |
| India | bseindia.com, nseindia.com | BSE / NSE |
| US | sec.gov, edgar.sec.gov, epa.gov, fdic.gov, nasdaq.com, otcmarkets.com, www.otcmarkets.com, archive.fast-edgar.com | SEC EDGAR / EPA / FDIC / NASDAQ / OTC Markets / FAST-EDGAR |
| Taiwan | twse.com.tw | TWSE |
| Israel | **tase.co.il**, mayafiles.tase.co.il | TASE / TASE Maya |
| Saudi Arabia | saudiexchange.sa | Tadawul |
| Southeast Asia | sgx.com, idx.co.id, bursamalaysia.com, set.or.th, pse.com.ph, sec.or.th, market.sec.or.th | Various exchanges / SEC Thailand |
| Australia/NZ | asx.com.au, nzx.com | ASX / NZX |
| UK | find-and-update.company-information.service.gov.uk, londonstockexchange.com, www.londonstockexchange.com, rns-pdf.londonstockexchange.com, www.rns-pdf.londonstockexchange.com | Companies House / LSE / LSE RNS |
| Europe | euronext.com, live.euronext.com, cnmv.es, amf-france.org | Euronext / CNMV (Spain) / AMF (France) |
| Americas (non-US) | sedarplus.ca, www.sedarplus.ca, bmv.com.mx | SEDAR+ (Canada) / BMV (Mexico) |
| South Africa | senspdf.jse.co.za, clientportal.jse.co.za | JSE SENS / JSE Client Portal |
| Turkey | kap.org.tr | KAP |
| IR Hosting | q4cdn.com, cloudfront.net, mziq.com, listedcompany.com, annualreports.com, markitdigital.com, publitas.com, azurefd.net, irwebpage.com | Various CDN/IR platforms |

> **IMPORTANT:** `tase.co.il` (Tel Aviv Stock Exchange) is a **regulatory portal**,
> NOT third-party. It was moved from THIRDPARTY_AGGREGATOR_DOMAINS to REPORT_HOST_DOMAINS
> based on analyst feedback. `mayafiles.tase.co.il` is the TASE Maya filing portal.

> **IMPORTANT:** `cloudfront.net` and `mziq.com` are in REPORT_HOST_DOMAINS (they host
> SEC filings and annual reports). They were moved FROM third-party based on analyst feedback.

### THIRDPARTY_AGGREGATOR_DOMAINS → URLC02

~100+ domains including registries (opencorporates.com, companieshouse.gov.uk),
exchange portals (jse.co.za), news/PR distributors
(prnewswire.com, businesswire.com), data aggregators (globaldata.com, morningstar.com),
job platforms (glassdoor.com, taleo.net), and store locator widgets (storemapper.co).

> **NOTE:** nasdaq.com, euronext.com, londonstockexchange.com variants, sedarplus.ca,
> bmv.com.mx, senspdf.jse.co.za, clientportal.jse.co.za, and other stock exchange
> domains were **moved to REPORT_HOST_DOMAINS** (regulatory portals) — they are NOT
> third-party. See the REPORT_HOST table above for the full list.

See `url_consistency_engine.py` lines 90-259 for the full annotated list.

### Guard: Report Host vs Third-Party Conflict

`_is_report_host()` has a critical guard:
```python
if _is_third_party_registry(url):
    return False  # Registry wins
```

**When moving a domain from third-party to report host, you MUST remove it from
the third-party list.** Otherwise the guard blocks it and the domain stays third-party.

---

## Subsidiary Logic

1. `Is_Subsidiary=1` flag from upstream does NOT auto-classify. The URL could be on
   a third-party site that merely mentions the subsidiary.
2. **Subsidiary + company domain match** → URLC01 (safe: URL is on the issuer's own domain)
3. **Subsidiary + NOT on company domain + NOT third-party** → depends on verdict mode:
   - v1/v3: Manual Review
   - v2: URLC01 (with explanation "not part of child issuer table")
4. **Child table match** (domain stem matches a child issuer's URL):
   - v1/v3: Manual Review (with suggested issuer ID)
   - v2: URLC03 (with suggested issuer ID to replace)

---

## Company URL Matching

The `company_url_col` parameter enables two checks:
1. **Stem match:** domain stem of RELEVANT_URL == domain stem of company URL
2. **Subdomain match:** URL root domain ends with company root domain
   (e.g., `ir.murphyoilcorp.com` matches `murphyoilcorp.com`)

> **Murphy Oil example:** `ir.murphyoilcorp.com` was wrongly Manual Review because
> no company URL column was provided. After enriching with parent company URL from the
> child table (`child_df.groupby('issuer_id')['url'].first()`), subdomain matching
> correctly classified it as URLC01.

When the input data has unreliable COMPANY_DOMAIN columns, pull the parent company
URL from the child table's `url` column instead.

---

## LLM Cache Format

Cache file: `cache/llm_verdicts.json`

**Storage format:** String keys `"ISSUER_ID|||domain_stem"` → `"URLC01"` or `"URLC02"`
```json
{
  "IID000000002123716|||murphyoilcorp": "URLC01",
  "IID000000001088223|||xueqiu": "URLC02"
}
```

**In-memory format:** `run_pipeline()` expects tuple keys `(ISSUER_ID, domain_stem)`.
The function `load_llm_cache()` in `pipeline_common.py` converts between formats:
```python
key.split("|||") → tuple(parts)
```

> **CRITICAL:** Never pass raw JSON dict to `run_pipeline()` — always use
> `load_llm_cache()` which does the string→tuple conversion. Without this,
> zero LLM verdicts get applied (keys don't match).

---

## Explanation System

`pipeline_common.py` contains `_REGULATORY_PORTAL_NAMES` — a dict mapping ~30+ domains
to human-readable portal names. When a URL matches a report host, the explanation shows
the specific portal:
```
"Regulatory filing / report host: TASE Maya (Tel Aviv Stock Exchange filings)"
```

Explanations are verdict-mode aware. In v2:
- MANUAL_REVIEW_SUBSIDIARY → "Subsidiary detected (upstream flag) — not part of child issuer table"
- MANUAL_REVIEW_CHILD_TABLE → includes "subsidiary in child issuer table, replace with correct issuer ID"

---

## Excel Formatting

Output files have color-coded verdict cells:
| Verdict | Text Color | Background |
|---------|-----------|------------|
| URLC01 | Dark green (#006100) | Light green (#C6EFCE) |
| URLC02 | Dark red (#9C0006) | Light red (#FFC7CE) |
| URLC03 | Dark blue (#00336B) | Light blue (#BDD7EE) |
| Manual Review | Dark yellow (#9C6500) | Light yellow (#FFEB9C) |

Features: frozen header row, auto-filter, column widths optimized, verdict column width = 22.

---

## Past Analyst Feedback (Incorporated)

These decisions were made based on analyst review and are baked into the rules:

1. **tase.co.il is regulatory**, not third-party (426 rows corrected)
2. **cloudfront.net hosts SEC filings** — moved from third-party to report host
3. **mziq.com is an IR platform** — moved from third-party to report host
4. **UK Companies House** (find-and-update.company-information.service.gov.uk) — moved from
   REGISTRY_DOMAINS to REPORT_HOST_DOMAINS (regulatory filings)
5. **Press release distributors** (prnewswire.com, businesswire.com, globenewswire.com) are
   third-party — content is ABOUT the company but not company-owned
6. **Store locator widgets** (storemapper.co, storepoint.co, etc.) are third-party SaaS
7. **Job platforms** (glassdoor.com, taleo.net, etc.) are third-party
8. **Majority domain alone is insufficient** — must also have name-match to issuer
   (prevents false URLC01 when data was bulk-collected from an aggregator)
9. **Subsidiary flag + third-party domain → needs review** — Is_Subsidiary=1 doesn't
   mean the URL is correct if it's on linkedin.com or globaldata.com
10. **Company URL enrichment from child table** — when COMPANY_DOMAIN is unreliable,
    use `child_df.groupby('issuer_id')['url'].first()` as the canonical company URL
11. **v2: Dictionary detections trusted, LLM detections need review** — in verdict mode v2,
    only dictionary-based rules (hardcoded domain lists) produce final URLC01/URLC02 verdicts.
    LLM-detected verdicts go to Manual Review first, then a post-processing step promotes
    company-owned ones (identified by explanation patterns like "company-owned", "own domain",
    "parent domain", "subsidiary", "Domain stem matches issuer name") back to URLC01.
    LLM-detected third-party sites (irbank, minedocs, costar, cbinsights, etc.) stay
    Manual Review until analysts validate and add them to the dictionary.
12. **gov.uk domain stem extraction returns None** — `extract_domain_stem()` strips all
    TLD noise from `gov.uk`, producing `None`. The Aramark/CMA case (gov.uk/cma-cases)
    was manually classified as URLC02 (third-party government content about the company).
    Consider adding `gov.uk` to the third-party list or fixing the stem extractor for
    two-part TLDs like `.gov.uk`, `.co.uk`, `.com.au`.

---

## Common Cowork Tasks

### "Run the pipeline on a new file"
```bash
# Standard (columns already named RELEVANT_URL, ISSUER_ID, ISSUER_NAME):
python scripts/run_pipeline.py --input input/data.xlsx --mode full --verdict-mode v2

# If columns need renaming (URL→RELEVANT_URL, issuer_id→ISSUER_ID, etc.)
# and company URL should come from child table, use run_custom.py:
python scripts/run_custom.py
# (edit run_custom.py to change input/output paths)
```

### "Run with column remapping + child-table company URL"
`scripts/run_custom.py` handles files where columns are lowercase (e.g. `URL`,
`issuer_id`, `company_name`). It renames them to match the engine's expected
format (`RELEVANT_URL`, `ISSUER_ID`, `ISSUER_NAME`) and enriches company URL
from `child_df.groupby('issuer_id')['url'].first()` instead of using the
input's `COMPANY_DOMAIN` column.

### "Classify unresolved LLM combos"
After running step 1, read unresolved combos and classify them in bulk using
pattern-based stem→verdict maps. Common patterns:
- Stems like `ir`, `investor`, `investors`, `corporate`, `static`, `assets`, `cdn`,
  `media`, `images`, `content` → URLC01 (company subdomains)
- Stems matching issuer name or abbreviation → URLC01
- Unknown stems → check if company subsidiary, otherwise URLC02

### "Add a new domain to report host / third-party"
1. Add to the appropriate list in `url_consistency_engine.py`
2. If moving between lists, **remove from the source list** (guard conflict)
3. Add to `_REGULATORY_PORTAL_NAMES` in `pipeline_common.py` for enriched explanations
4. Re-run pipeline to apply

### "Fix a misclassification"
1. Identify the `auto_decision` for the row
2. Check which rule fired — is the domain in the wrong list?
3. If it's an LLM verdict issue, update `cache/llm_verdicts.json`
4. If it's a domain list issue, move the domain and re-run

---

## Environment Setup

```bash
pip install pandas openpyxl anthropic --break-system-packages
```

For API-based LLM calls, set in `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

The virtual environment at `myenv/` has dependencies pre-installed.
