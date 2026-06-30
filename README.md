# URL Consistency Check Pipeline

Automated 3-layer classification engine for URL consistency failures in MSCI's
physical asset / issuer data. Given a URL flagged as inconsistent with an issuer,
the pipeline decides whether the URL is **valid** (URLC01), **third-party** (URLC02),
a **subsidiary match** (URLC03), or needs **manual review**.

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Set your API key (only needed for new/uncached combos)
cp .env.example .env
# Edit .env → add your Anthropic, OpenAI, or Azure key

# 3. Place data in input/ and run
python scripts/run_pipeline.py --input input/your_data.xlsx --verdict-mode v2 --provider anthropic
```

## Folder Structure

```
URL_CONSITENCY_CHECK/
├── scripts/
│   ├── url_consistency_engine.py   # Core engine (domain lists, deterministic rules)
│   ├── pipeline_common.py          # Shared utilities (cache, explanations, Excel formatting)
│   ├── run_pipeline.py             # Main entry point (use this)
│   ├── run_custom.py               # Custom runner for non-standard column names
│   ├── run_full_check.py           # Standalone: classify ALL rows
│   ├── run_flagged_check.py        # Standalone: classify URL_CONSISTENCY_CHECK==1 only
│   └── run_cowork_check.py         # Cowork mode (Claude-in-session as LLM)
├── input/
│   └── issuer_child_flagged.xlsx   # Child/subsidiary table (REQUIRED)
├── output/                         # Results written here
├── cache/
│   ├── llm_verdicts.json           # LLM verdict cache (12,800+ entries)
│   └── llm_reasons.json            # LLM reason cache
├── .env                            # Your API keys (create from .env.example)
├── .env.example                    # Template
├── requirements.txt
├── CLAUDE.md                       # Full project knowledge base
└── README.md                       # This file
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API key

Copy `.env.example` to `.env` and fill in your key for one provider:

```bash
cp .env.example .env
```

**Anthropic (recommended):**
```
ANTHROPIC_API_KEY=sk-ant-api03-YOUR_KEY_HERE
```

**OpenAI:**
```
OPENAI_API_KEY=sk-YOUR_KEY_HERE
```

**Azure OpenAI:**
```
AZURE_OPENAI_API_KEY=YOUR_KEY_HERE
AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

**Google Cloud Vertex AI (MSCI recommended):**
```
GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-service-account.json
VERTEX_PROJECT_ID=proj-dg-dt-datacollqpt001-msci
VERTEX_REGION=global
```
Place your service account JSON key file in the project root (never commit it to git).
Available models: `claude-sonnet-4-6` (default), `claude-haiku-4-5`, `claude-sonnet-4-5`, `claude-opus-4-7`.

The LLM is only called for ambiguous issuer-domain combos not already in the cache.
The cache ships with 12,800+ pre-classified verdicts, so most runs need zero API calls.

### 3. Place your input file in `input/`

Supported formats: `.xlsx`, `.csv`, `.pkl`

Required columns: `ISSUER_ID`, `ISSUER_NAME`, `RELEVANT_URL`

Optional columns: `URL_IS_SUBSIDIARY` (or `Is_Subsidiary`), `FACILITY_NAME`,
`URL_CONSISTENCY_CHECK` (needed for `--mode flagged`),
`URL_CONSISTENCY_SUGGESTED_ISSUER_ID` (upstream suggestion)

If your file uses different column names (e.g. `URL` instead of `RELEVANT_URL`),
see "Custom Column Names" below.

## Verdict Modes

The pipeline supports five output modes via `--verdict-mode`:

### v1 (default) — Conservative

| Verdict | Meaning |
|---------|---------|
| URLC01 | Valid — URL belongs to issuer |
| Manual Review | Needs analyst review (all third-party + unclear) |

### v2 — Granular

| Verdict | Meaning |
|---------|---------|
| URLC01 | Valid — URL belongs to issuer (dictionary-confirmed OR LLM-detected company-owned) |
| URLC02 | Third-party — dictionary-confirmed (generic, registry, aggregator) |
| URLC03 | Subsidiary found in child_issuers table — replace with correct issuer ID |
| Manual Review | LLM-detected third-party — not yet in dictionary, needs analyst review |

v2 uses a 3-stage flow: (1) dictionary rules produce trusted verdicts, (2) LLM verdicts
start as Manual Review, (3) post-processing promotes company-owned LLM rows back to URLC01
based on explanation patterns (e.g. "subsidiary", "company-owned", "own domain").

### v3 — Flag==1 mode

Same as v1 (URLC01 + Manual Review only). Designed for `URL_CONSISTENCY_FLAG==1` rows.
All URLC02 detections are mapped to Manual Review — no third-party category in output.

### v4 — Strict Company-Only + Tag Column

| Verdict | Meaning |
|---------|---------|
| URLC01 | URL directly belongs to the company itself (not subsidiaries) |
| URLC02 | ALL third-party URLs including stock exchanges, gov registries, and aggregators |
| Manual_Review | URL belongs to a subsidiary (whether in child table or not) |

v4 adds a **Tag** column describing the corporate relationship between the URL's entity
and the issuer: `real_subsidiary_with_issuer_id`, `real_subsidiary_without_issuer_id`,
`ultimate_parent`, `parent`, `affiliate_not_subsidiary`, `unrelated_entity`.

Key differences: report hosts/regulatory portals → URLC02 (with "Part of allowed list" remark),
all subsidiaries → Manual_Review, LLM-detected subsidiaries demoted from URLC01 to Manual_Review.

### v5 — Strict Company-Only + Allowlist (URLC03) + Tag Column

Like v4, but **allowlisted** regulatory/exchange/government portals are classified as **URLC03**
instead of URLC02.

| Verdict | Meaning |
|---------|---------|
| URLC01 | URL on the issuer's own domain (direct company site, company-hosted PDFs/reports) |
| URLC03 | **Allowlisted** third-party — SEC EDGAR, stock exchanges, government filing portals, approved IR/CDN hosts (`REPORT_HOST_DOMAINS`) |
| URLC02 | **Non-allowlisted** third-party — generic sites, commercial registries, data aggregators, social media, etc. |
| Manual_Review | Subsidiaries (in or not in child table), upstream suggestions, unresolved cases |

v5 includes the same **Tag** column as v4 (`real_subsidiary_with_issuer_id`, `ultimate_parent`,
`unrelated_entity`, etc.).

**Allowlist examples (URLC03):** `sec.gov`, `nasdaq.com`, `londonstockexchange.com`,
`edinet-fsa.go.jp`, `find-and-update.company-information.service.gov.uk`, `cloudfront.net`,
`q4cdn.com`.

**Not on allowlist (URLC02):** `opencorporates.com`, `linkedin.com`, `ekstatic.net`, etc.

Rows with `URL_CONSISTENCY_CHECK == 0` are stamped **Don't validate** and skipped in all modes.

## How to Run

### Basic (cache-only, no API key needed)

```bash
# v1 — conservative (URLC01 + Manual Review)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v1

# v2 — granular (URLC01 + URLC02 + URLC03 + Manual Review)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2

# v3 — flag mode (URLC01 + Manual Review, no URLC02)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v3

# v4 — strict (URLC01 + URLC02 + Manual_Review + Tag column)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v4

# v5 — like v4, but allowlisted regulatory/exchange/gov portals → URLC03 + Tag column
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v5
```

### With LLM verification (resolves uncached combos via API)

```bash
# Google Cloud Vertex AI (MSCI recommended)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2 --provider vertex

# Vertex AI with specific model
python scripts/run_pipeline.py --input input/data.xlsx --provider vertex --model claude-haiku-4-5

# Anthropic Claude (direct API)
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2 --provider anthropic

# OpenAI GPT-4o
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2 --provider openai

# Azure OpenAI
python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2 --provider azure
```

### Mode: Full vs Flagged

```bash
# Flagged (default) — only URL_CONSISTENCY_CHECK==1 rows
python scripts/run_pipeline.py --input input/data.xlsx --mode flagged --verdict-mode v3

# Full — classify ALL rows regardless of flag
python scripts/run_pipeline.py --input input/data.xlsx --mode full --verdict-mode v2
```

### With company URL column (enables subdomain matching)

```bash
python scripts/run_pipeline.py --input input/data.xlsx --company-url-col COMPANY_URL --verdict-mode v2
```

### Skip LLM entirely

```bash
python scripts/run_pipeline.py --input input/data.xlsx --skip-llm --verdict-mode v2
```

### Custom output path

```bash
python scripts/run_pipeline.py --input input/data.xlsx --output output/my_results.xlsx --verdict-mode v2
```

## Custom Column Names

If your input file has non-standard column names (e.g. `URL` instead of `RELEVANT_URL`,
`issuer_id` instead of `ISSUER_ID`), edit `scripts/run_custom.py`:

```python
# In run_custom.py, update the rename map:
rename_map = {
    'URL': 'RELEVANT_URL',
    'issuer_id': 'ISSUER_ID',
    'company_name': 'ISSUER_NAME',
}
```

Then run:
```bash
python scripts/run_custom.py
```

`run_custom.py` also enriches company URL from the child issuer table
(`child_df.groupby('issuer_id')['url'].first()`) instead of using the input's
`COMPANY_DOMAIN` column, which may be unreliable.

## How It Works

### 3-Layer Pipeline

```
Layer 1 — Deterministic Rules (instant, 100% precision)
  ├── Generic URL check (Google Maps, LinkedIn, etc.) → URLC02
  ├── Third-party registry check (registries, aggregators) → URLC02
  ├── Report host / regulatory portal check → URLC01
  ├── Child table match (parent stem → URLC01, child stem → Manual Review)
  ├── Company URL column match (stem + subdomain) → URLC01
  ├── Subsidiary flag + company domain guard → URLC01
  └── Upstream suggestion → Manual Review

Layer 2 — Majority Domain (heuristic, name-verified only)
  └── If domain is the most common for an issuer AND matches name → URLC01

Layer 3 — LLM / Cache Verification
  └── Keyed by (ISSUER_ID, domain_stem) → URLC01 or URLC02
      Cache: cache/llm_verdicts.json (12,800+ entries)
```

### LLM Verification Flow (when `--provider` is specified)

1. Collapses unresolved rows into unique issuer+domain combos
2. Runs web search (DuckDuckGo) to gather evidence
3. Sends structured prompt to LLM with search evidence
4. Caches verdict + reason in `cache/` for future runs
5. Re-applies verdicts — no repeat API calls

### Output

Results are saved to `output/results.xlsx` (or you