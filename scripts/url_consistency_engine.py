"""
URL Consistency Check Engine
============================
Automated classification of URL consistency failures into:
  URLC01 = Valid (URL is correct, mismatch is explainable)
  URLC02 = Invalid (URL genuinely doesn't belong to that company)

Pipeline:
  Layer 1 — Deterministic rules (child table, report hosts, generic URLs, subsidiary flag)
  Layer 2 — Majority domain matching within issuer
  Layer 3 — LLM verification via web search for remaining ambiguous combos

Usage:
  from url_consistency_engine import run_pipeline, get_llm_review_combos

  result = run_pipeline(main_df, child_df)
  combos = get_llm_review_combos(result)       # ~101 combos for LLM
  # ... run LLM on combos, collect verdicts ...
  result = run_pipeline(main_df, child_df, llm_verdicts=verdicts_dict)
"""

import re
import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional, Callable

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# =============================================================================
#  UTILITY FUNCTIONS
# =============================================================================

def extract_root_domain(url: str) -> Optional[str]:
    """Extract root domain from URL, handling country-code SLDs like .co.kr"""
    if pd.isna(url):
        return None
    m = re.search(r'https?://(?:www\d?\.)?([^/]+)', str(url).lower())
    if not m:
        return None
    host = m.group(1)
    parts = host.split('.')
    cc_slds = ['co', 'com', 'org', 'net', 'gov', 'ac', 'or']
    if len(parts) >= 3 and parts[-2] in cc_slds:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def extract_domain_stem(url: str) -> Optional[str]:
    """Extract the meaningful 'stem' of a domain (no TLD, no www)."""
    if pd.isna(url):
        return None
    m = re.search(r'https?://(?:www\d?\.)?([^/]+)', str(url).lower())
    if not m:
        return None
    host = m.group(1)
    parts = host.split('.')
    tld_noise = [
        'com', 'org', 'net', 'co', 'gov', 'ac', 'or',
        'cn', 'tw', 'kr', 'jp', 'hk', 'in', 'my', 'br', 'za', 'ae',
        'tr', 'uk', 'au', 'nz', 'sg', 'th', 'id', 'ph', 'vn', 'io',
        'mx', 'il', 'sa', 'ru', 'de', 'fr', 'es', 'it', 'nl', 'se',
        'no', 'dk', 'fi', 'pl', 'cz', 'pt', 'ro', 'hu', 'bg', 'hr',
    ]
    stem_parts = [p for p in parts if p not in tld_noise and p != 'www' and len(p) > 1]
    return stem_parts[0] if stem_parts else None


# =============================================================================
#  LAYER 1 — DETERMINISTIC RULES
# =============================================================================

# Curated list of generic / non-company-specific domains → always URLC02
GENERIC_URL_PATTERNS = [
    'google.com/maps', 'google.com/search', 'maps.google.com',
    'script.google.com', 'docs.google.com', 'drive.google.com',
    'drive.usercontent.google.com', 'gemini.google.com',
    'linkedin.com', 'bloomberg.com',
    'pitchbook.com', 'dnb.com', 'waze.com', 'facebook.com',
    'twitter.com', 'wikipedia.org', 'youtube.com',
    'instagram.com', 'tiktok.com',
    'openstreetmap.org',       # Map data — generic, not company-owned
]

# ── THIRD-PARTY REGISTRIES & PORTALS → URLC02 ──────────────────────────────
# These sites *list* company information but are NOT owned by the company.
# Key distinction: a government registry page *about* a subsidiary ≠ the company's own site.
# Validated against analyst feedback (url_qa_validation_sample.xlsx).

# Business / Company Registries (government & commercial)
REGISTRY_DOMAINS = [
    # company-information.service.gov.uk moved to REPORT_HOST_DOMAINS as regulatory portal
    'virksomhet.brreg.no',       # Norwegian Brønnøysund Register Centre
    'econodata.com.br',          # Brazilian business registry
    'moneyhouse.ch',             # Swiss business registry
    'northdata.com',             # German business registry
    'cleartax.in',               # Indian tax/company registry
    'opencorporates.com',        # Global business registry aggregator
    'instafinancials.com',       # Indian company financials registry
    'globaldatabase.com',        # Global business registry (ie.globaldatabase.com, etc.)
    'datalog.co.uk',             # UK company data
    'lursoft.lv',                # Latvian business registry
    'societe.com',               # French business registry
    'firmenwissen.de',           # German business registry
    'zefix.ch',                  # Swiss official company register
    'handelsregister.de',        # German commercial register
    'kvk.nl',                    # Dutch Chamber of Commerce
    'proff.no',                  # Norwegian company registry
    'proff.se',                  # Swedish company registry
    'proff.dk',                  # Danish company registry
    'allabolag.se',              # Swedish company registry
    'pappers.fr',                # French business registry
    'infocif.es',                # Spanish company registry
    'companieshouse.gov.uk',     # UK Companies House (alt)
]

# Stock Exchange Filing Portals & Regulatory Registries
EXCHANGE_PORTAL_DOMAINS = [
    # jse.co.za moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    # londonstockexchange.com moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    # euronext.com moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    'files.brokercheck.finra.org',         # FINRA BrokerCheck filings
    'brokercheck.finra.org',               # FINRA BrokerCheck
    'borsaitaliana.it',                    # Borsa Italiana
    'b3.com.br',                           # B3 Brazilian Stock Exchange
    'mzgroup.com',                         # MZ Group IR services portal
]

# Third-Party Data Providers & Aggregators
THIRDPARTY_AGGREGATOR_DOMAINS = [
    'globaldata.com',            # GlobalData company profiles
    'publicnow.com',             # PublicNow document aggregator
    'financialreports.eu',       # Financial report hosting
    'reuters.com',               # Reuters (news, not company-owned)
    'morningstar.com',           # Fund/stock data
    'marketscreener.com',        # Stock market data
    'wisesheets.io',             # Financial data sheets
    'simplywall.st',             # Stock analysis
    'macrotrends.net',           # Financial data
    'companiesmarketcap.com',    # Market cap rankings
    # Analyst feedback batch — newswire / PR distribution (content ABOUT company, not company-owned)
    'prnewswire.com',            # PR Newswire — press release distributor
    'globenewswire.com',         # GlobeNewsWire — press release distributor
    'businesswire.com',          # Business Wire — press release distributor
    'stocktitan.net',            # Stock Titan — press release distributor
    'eqs-news.com',              # EQS News — IR news distributor
    'mailing-ircockpit.eqs.com', # EQS IR cockpit
    # Analyst feedback batch — trade data / supply chain aggregators
    'panjiva.com',               # Panjiva — supply chain data aggregator
    'volza.com',                 # Volza — import/export data aggregator
    'echemi.com',                # Echemi — chemical trade aggregator
    # Analyst feedback batch — IR hosting / document CDNs (not company-owned)
    'irasia.com',                # IR Asia — IR hosting service
    'ir-service.net',            # IR Service — third-party IR document CDN
    'ekstatic.net',              # Emirates Group static CDN (hosts PDFs for EK entities, not company-owned)
    'insage.com.my',             # InSage — Malaysian IR portal
    'itcportal.com',             # ITC Portal — IR hosting
    'irwebcasting.com',          # IR webcasting — event hosting
    # mziq.com moved to REPORT_HOST_DOMAINS (regulatory portal / IR hosting)
    # Analyst feedback batch — third-party news / data / directories
    'scribd.com',                # Scribd — document sharing platform
    'nzherald.co.nz',            # NZ Herald — news
    'coindesk.com',              # CoinDesk — crypto news
    'ibisworld.com',             # IBISWorld — industry research
    'baxtel.com',                # Baxtel — data center directory
    'highperformr.ai',           # Highperformr — B2B data aggregator
    'komachine.com',             # Komachine — Korean machinery directory
    'nikkei.com',                # Nikkei — Japanese financial data/news
    'devex.com',                 # Devex — development sector aggregator
    'captivereview.com',         # Captive Review — industry magazine
    'britama.com',               # Britama — Indonesian financial profiles
    'emitennews.com',            # EmitenNews — Indonesian financial news
    'idxchannel.com',            # IDX Channel — Indonesian financial news
    'en-gage.net',               # en-gage — Japanese recruitment platform
    'made-in-china.com',         # Made-in-China — Chinese product directory
    'hktdc.com',                 # HKTDC — HK trade directory
    'yellowpages.co.th',         # Thai Yellow Pages directory
    'ifdesign.com',              # iF Design — awards directory
    'thegazette.com',            # The Gazette — news
    'pharmaceutical-technology.com',  # Pharma Technology — industry news
    'petfoodprocessing.net',     # Pet Food Processing — industry news
    'feedstrategy.com',          # Feed Strategy — industry news
    'wattagnet.com',             # WattAgNet — poultry industry news
    'pharmasource.com.au',       # PharmaSource — industry directory
    'cmocro.com',                # CMO/CRO Directory
    'mexicopymes.com',           # Mexico PYMES — business directory
    'newpages.com.my',           # NewPages — Malaysian business directory
    'nordjyskebank.dk',          # Nordjyske Bank (separate entity)
    'thescirank.com',            # TheSciRank — academic ranking
    'stuff.co.nz',               # Stuff — NZ news
    'opengovsg.com',             # OpenGovSG — Singapore government data
    'ogj.com',                   # Oil & Gas Journal — industry news
    'pv-magazine.com',           # PV Magazine — solar industry news
    'therealdeal.com',           # The Real Deal — real estate news
    'travelandtourworld.com',    # Travel & Tour World — industry news
    'stratcann.com',             # Stratcann — cannabis news
    'strategy-advisors.co.jp',   # Strategy Advisors — consulting
    'fooddive.com',              # Food Dive — industry news
    'builtin.com',               # BuiltIn — tech company profiles
    'lightwaveonline.com',       # Lightwave — telecom industry
    'salestools.io',             # SalesTools — B2B data
    'realxen.com',               # RealXen — real estate data
    'hcvnetwork.org',            # HCV Network — conservation NGO
    'physiotherabia.com',        # Physiotherabia — healthcare directory
    'sunpharma.com',             # Sun Pharma (separate company)
    'tokaitokyo.co.jp',          # Tokai Tokyo (separate entity)
    'sigplc.com',                # SIG PLC (separate entity)
    'arganinc.com',              # Argan Inc (separate company)
    'athexgroup.gr',             # Athens Stock Exchange
    # bmv.com.mx moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    # nasdaq.com moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    'hkma.gov.hk',               # HK Monetary Authority — regulator
    'rspo.org',                  # RSPO — sustainability certification
    # Cloud hosting CDNs (third-party infra, not company-owned domains)
    # cloudfront.net moved to REPORT_HOST_DOMAINS (hosts SEC filings, annual reports)
    'amazonaws.com',             # AWS S3 buckets
    'feeds.dfm.ae',              # Dubai Financial Market feeds
    # Store locator / map widget platforms (third-party SaaS, not company-owned)
    'storemapper.co',            # StoreMapper — store locator widget
    'storemapper.com',           # StoreMapper — store locator widget (alt)
    'storerocket.io',            # StoreRocket — store locator widget
    'storepoint.co',             # Storepoint — store locator widget
    'metalocator.com',           # Metalocator — store/dealer locator
    'brandmaster.com',           # BrandMaster — brand management platform
    # Job / career / HR platforms (third-party, not company-owned)
    'taleo.net',                 # Oracle Taleo — recruitment platform
    'glassdoor.com',             # Glassdoor — job reviews
    'ultipro.com',               # UltiPro / UKG — HR platform
    'successfactors.com',        # SAP SuccessFactors — HR platform
    'careerarc.com',             # CareerArc — job distribution
    'pageuppeople.com',          # PageUp — recruitment platform
    'jobs2web.com',              # Jobs2Web — recruitment marketing
    'oraclecloud.com',           # Oracle Cloud — HR/ERP platform
    'fbcareers.com',             # Facebook/Meta careers (third-party job board)
    # IR / investor hosting platforms (third-party, not company-owned)
    'irpocket.com',              # IR Pocket — Japanese IR hosting
    # sedarplus.ca moved to REPORT_HOST_DOMAINS (regulatory filing portal)
    'mfn.se',                    # MFN — Modular Finance news distribution
    'chartnexus.com',            # ChartNexus — financial charting
    'cision.com',                # Cision — PR/IR distribution
    'yourir.info',               # YourIR — IR hosting
    # tase.co.il moved to REPORT_HOST_DOMAINS (Israel regulatory filing portal)
    'futunn.com',                # Futu — HK/US stock trading platform
    'tofler.in',                 # Tofler — Indian company data
    # Website/hosting/widget platforms (generic infra)
    'website-files.com',         # Webflow hosted files
    'app-platform.io',           # DigitalOcean App Platform
    'weblink.com.br',            # Weblink — Brazilian IR platform
    'algolia.net',               # Algolia — search widget
    'kc-usercontent.com',        # Kentico Cloud user content CDN
    'sharelinktechnologies.com', # ShareLink — IR hosting
    'storage.yahoo-net.jp',      # Yahoo Japan storage CDN
    'xj-storage.jp',             # XJ Storage — Japanese document hosting
    'metoree.com',               # Metoree — Japanese industrial product directory
]

# Combined: all third-party registries/portals → URLC02
THIRD_PARTY_REGISTRY_DOMAINS = (
    REGISTRY_DOMAINS + EXCHANGE_PORTAL_DOMAINS + THIRDPARTY_AGGREGATOR_DOMAINS
)

# ── REPORT HOST DOMAINS → URLC01 ────────────────────────────────────────────
# These are sites where the COMPANY ITSELF files/uploads documents.
# Distinction from registries: the company chose to publish here, and the content
# represents the company's own disclosures (annual reports, investor presentations).
# NOTE: Stock exchanges removed — analyst feedback says exchange portals are URLC02.
REPORT_HOST_DOMAINS = [
    # ── Regulatory / Stock Exchange Filing Portals (URLC01) ──
    # These are official regulatory portals where issuers file documents.
    # A URL on these domains belongs to the issuer → URLC01.

    # China
    'cninfo.com.cn',                  # China CSRC / Shenzhen Stock Exchange
    'static.cninfo.com.cn',           # China CSRC static CDN
    'sse.com.cn',                     # Shanghai Stock Exchange

    # Japan
    'go.jp',                          # Japan EDINET (edinet-fsa.go.jp) and other govt
    'edinet-fsa.go.jp',               # Japan EDINET explicit
    'disclosure2dl.edinet-fsa.go.jp', # EDINET direct download CDN
    'jpx.co.jp',                      # Japan Exchange Group

    # Korea
    'or.kr',                          # Korea DART (dart.fss.or.kr) and related
    'dart.fss.or.kr',                 # Korea DART explicit

    # Hong Kong
    'hkexnews.hk',                    # Hong Kong Exchange filings

    # India
    'bseindia.com',                   # India Bombay Stock Exchange
    'nseindia.com',                   # India National Stock Exchange
    'nsearchives.nseindia.com',       # NSE India archives
    'nse-india.com',                  # NSE India (alt domain)

    # US
    'sec.gov',                        # US SEC EDGAR
    'edgar.sec.gov',                  # SEC EDGAR direct
    'epa.gov',                        # US EPA environmental filings & facility data
    'fdic.gov',                       # US FDIC bank regulatory data

    # Taiwan
    'twse.com.tw',                    # Taiwan Stock Exchange
    'doc.twse.com.tw',                # TWSE document portal

    # Middle East / Israel
    'saudiexchange.sa',               # Saudi Exchange (Tadawul)
    'tase.co.il',                     # Tel Aviv Stock Exchange (TASE)
    'mayafiles.tase.co.il',           # TASE Maya regulatory filings portal

    # Southeast Asia
    'pse.com.ph',                     # Philippine Stock Exchange
    'edge.pse.com.ph',                # PSE edge portal
    'idx.co.id',                      # Indonesia Stock Exchange
    'bursamalaysia.com',              # Malaysia Bursa
    'sgx.com',                        # Singapore SGX
    'set.or.th',                      # Stock Exchange of Thailand
    'sec.or.th',                      # Securities and Exchange Commission, Thailand
    'market.sec.or.th',               # SEC Thailand market filings

    # Australia / New Zealand
    'asx.com.au',                     # Australia ASX
    'nzx.com',                        # New Zealand NZX

    # UK
    'find-and-update.company-information.service.gov.uk',  # UK Companies House (regulatory filings)
    'find-and-update.company',        # UK Companies House (short domain)
    'londonstockexchange.com',        # London Stock Exchange
    'www.londonstockexchange.com',    # London Stock Exchange (www)
    'www.rns-pdf.londonstockexchange.com',  # LSE Regulatory News Service PDFs
    'rns-pdf.londonstockexchange.com',      # LSE Regulatory News Service PDFs

    # Turkey
    'kap.org.tr',                     # Turkey Public Disclosure Platform

    # Europe
    'euronext.com',                   # Euronext exchange (Amsterdam/Paris/Lisbon/Dublin)
    'live.euronext.com',              # Euronext live filings
    'cnmv.es',                        # CNMV (Spanish Securities Commission)
    'amf-france.org',                 # AMF (French Financial Markets Authority)

    # Americas
    'nasdaq.com',                     # NASDAQ exchange filings
    'otcmarkets.com',                 # OTC Markets Group
    'www.otcmarkets.com',             # OTC Markets Group (www)
    'sedarplus.ca',                   # SEDAR+ (Canadian securities filings)
    'www.sedarplus.ca',               # SEDAR+ (www)
    'bmv.com.mx',                     # BMV (Bolsa Mexicana de Valores)
    'archive.fast-edgar.com',         # FAST-EDGAR (SEC filing archive)

    # Korea
    'kind.krx.co.kr',                 # KIND (Korea Exchange disclosure)

    # South Africa
    'senspdf.jse.co.za',              # JSE SENS PDF filings
    'clientportal.jse.co.za',         # JSE Client Portal

    # ── IR Hosting / Document CDN Platforms ──
    # Issuers upload their own filings/reports to these — treat as URLC01.
    'listedcompany.com',              # Company-uploaded annual reports
    'annualreports.com',              # Annual report repository
    'cloudfront.net',                 # AWS CloudFront (hosts SEC filings, annual reports)
    'markitdigital.com',              # Markit Digital (ASX research / CDN)
    'mziq.com',                       # MZ Group (Brazil IR platform)
    'q4cdn.com',                      # Q4 Inc. IR document hosting
    's22.q4cdn.com',                  # Q4 CDN subdomains
    's23.q4cdn.com',
    's24.q4cdn.com',
    's25.q4cdn.com',
    's26.q4cdn.com',
    's27.q4cdn.com',
    's28.q4cdn.com',
    'publitas.com',                   # Publitas digital publication CDN
    'azurefd.net',                    # Azure Front Door CDN
    'irwebpage.com',                  # IR Webpage platform

    # Other
    'gem.wiki',                       # Global Energy Monitor wiki
]

# Known generic hosting domains → NEEDS_LLM_REVIEW (ambiguous; companies often use
# cloud hosting for legitimate content, so we can't auto-reject these)
# NOTE: cloudfront.net is now in REPORT_HOST_DOMAINS (regulatory/IR hosting CDN).
GENERIC_HOSTING_PATTERNS = [
    # Intentionally kept minimal; most "generic" hosts are used legitimately.
    # These are sent to LLM review, NOT auto-rejected.
]


def _is_generic_url(url: str) -> bool:
    """Check if URL is a generic third-party site (Google Maps, LinkedIn, etc.)."""
    if pd.isna(url):
        return False
    url_lower = str(url).lower()
    return any(p in url_lower for p in GENERIC_URL_PATTERNS)


def _is_third_party_registry(url: str) -> bool:
    """Check if URL is a third-party registry, exchange portal, or data aggregator.
    These sites LIST company info but are NOT owned by the company → URLC02."""
    if pd.isna(url):
        return False
    url_lower = str(url).lower()
    return any(d in url_lower for d in THIRD_PARTY_REGISTRY_DOMAINS)


def _is_generic_hosting(url: str) -> bool:
    """Check if URL is on a generic hosting platform (not company-owned).
    Currently disabled — companies legitimately use cloud hosting for PDFs."""
    return False  # Disabled: too many false positives (S3-hosted reports, etc.)


def _is_report_host(url: str) -> bool:
    """Check if URL is a known financial filing / report hosting site."""
    if pd.isna(url):
        return False
    url_lower = str(url).lower()
    # Registry check takes priority — if it's a registry, it's NOT a report host
    if _is_third_party_registry(url):
        return False
    return any(h in url_lower for h in REPORT_HOST_DOMAINS)


def build_child_lookup(child_df: pd.DataFrame) -> dict:
    """
    Build a per-issuer lookup of known child/parent domain stems + child names.

    Returns dict keyed by issuer_id → {child_names, child_stems, parent_stems, all_stems,
                                        stem_to_child_info, name_to_child_info}
    Each child info entry is a dict with: child_issuer_id, child_name, child_url
    """
    lookup = {}
    for issuer_id, grp in child_df.groupby('issuer_id'):
        child_names = set(grp['child_name'].dropna().str.lower().tolist())
        child_stems = set(grp['child_url'].apply(extract_domain_stem).dropna().tolist())
        parent_stems = set(grp['url'].apply(extract_domain_stem).dropna().tolist())

        # Map each child stem → full child info (id, name, url)
        stem_to_child_info = {}
        for _, row in grp.iterrows():
            cstem = extract_domain_stem(row.get('child_url', ''))
            cid = row.get('child_issuer_id', '')
            cname = row.get('child_name', '')
            curl = row.get('child_url', '')
            if cstem and not pd.isna(cid):
                stem_to_child_info[cstem] = {
                    'child_issuer_id': str(cid),
                    'child_name': str(cname) if not pd.isna(cname) else '',
                    'child_url': str(curl) if not pd.isna(curl) else '',
                }

        # Map each child name → full child info
        name_to_child_info = {}
        for _, row in grp.iterrows():
            cname_key = str(row.get('child_name', '')).lower().strip()
            cid = row.get('child_issuer_id', '')
            cname = row.get('child_name', '')
            curl = row.get('child_url', '')
            if cname_key and not pd.isna(cid):
                name_to_child_info[cname_key] = {
                    'child_issuer_id': str(cid),
                    'child_name': str(cname) if not pd.isna(cname) else '',
                    'child_url': str(curl) if not pd.isna(curl) else '',
                }

        lookup[issuer_id] = {
            'child_names': child_names,
            'child_stems': child_stems,
            'parent_stems': parent_stems,
            'all_stems': child_stems | parent_stems,
            'stem_to_child_info': stem_to_child_info,
            'name_to_child_info': name_to_child_info,
        }
    return lookup


def _check_child_table(issuer_id: str, domain_stem: str, child_lookup: dict) -> tuple:
    """
    Check if domain stem is present in issuer's child/parent URLs or names.
    Returns (match_type, child_info_dict_or_None).

    Match types:
      PARENT_STEM_MATCH — domain matches the issuer's OWN url → URLC01
      CHILD_STEM_MATCH  — domain matches a subsidiary/child url → Manual Review
      NAME_PARTIAL      — domain partially matches a child company name → Manual Review
      NO_MATCH / NO_DATA — no match found
    """
    if pd.isna(domain_stem) or issuer_id not in child_lookup:
        return ('NO_DATA', None)
    info = child_lookup[issuer_id]

    # Check parent stems FIRST — if URL matches the issuer's own domain, it's URLC01
    if domain_stem in info['parent_stems']:
        # It's the issuer's own URL, not a subsidiary
        return ('PARENT_STEM_MATCH', None)

    # Check child stems — if URL matches a subsidiary domain, needs review
    if domain_stem in info['child_stems']:
        child_info = info['stem_to_child_info'].get(domain_stem)
        return ('CHILD_STEM_MATCH', child_info)

    # Partial: domain stem appears inside a child company name (or vice versa)
    for cn in info['child_names']:
        tokens = cn.split()
        if domain_stem in cn or (tokens and tokens[0] in domain_stem and len(tokens[0]) > 3):
            child_info = info['name_to_child_info'].get(cn)
            return ('NAME_PARTIAL', child_info)
    return ('NO_MATCH', None)


def apply_deterministic_rules(df: pd.DataFrame, child_lookup: dict,
                              company_url_col: Optional[str] = None) -> pd.DataFrame:
    """
    Apply Layer 1+2 deterministic rules.

    Adds columns:
      - domain_stem: extracted domain stem from RELEVANT_URL
      - auto_decision: rule-based classification result

    Parameters
    ----------
    company_url_col : str, optional
        Name of a column containing the issuer's known/official company URL.
        If provided, domain stem comparison against this column is used as an
        additional high-priority rule (before majority domain).
    """
    df = df.copy()
    df['domain_stem'] = df['RELEVANT_URL'].apply(extract_domain_stem)

    # Rule 1: Generic URL → URLC02
    df['_r_generic'] = df['RELEVANT_URL'].apply(_is_generic_url)

    # Rule 1b: Third-party registry / exchange portal / data aggregator → URLC02
    # These sites LIST company info but are NOT owned by the company.
    # HIGHEST priority URLC02 rule — overrides child table, subsidiary flag, etc.
    df['_r_registry'] = df['RELEVANT_URL'].apply(_is_third_party_registry)

    # Rule 1c: Generic hosting → URLC02
    df['_r_generic_host'] = df['RELEVANT_URL'].apply(_is_generic_hosting)

    # Rule 2: Report host → URLC01
    df['_r_report'] = df['RELEVANT_URL'].apply(_is_report_host)

    # Rule 3: Child table match — returns (match_type, child_info_dict)
    _child_results = df.apply(
        lambda r: _check_child_table(r['ISSUER_ID'], r['domain_stem'], child_lookup), axis=1
    )
    df['_r_child'] = _child_results.apply(lambda x: x[0])
    df['_matched_child_issuer_id'] = _child_results.apply(
        lambda x: x[1]['child_issuer_id'] if x[1] else None)
    df['_matched_child_name'] = _child_results.apply(
        lambda x: x[1]['child_name'] if x[1] else None)
    df['_matched_child_url'] = _child_results.apply(
        lambda x: x[1]['child_url'] if x[1] else None)

    # Rule 4: Subsidiary flag already set by upstream system
    # IMPORTANT: Is_Subsidiary alone does NOT mean the URL belongs to the issuer.
    # The URL could be on a third-party site that merely mentions the subsidiary.
    # We only auto-classify if the URL is NOT on a known third-party domain.
    if 'URL_IS_SUBSIDIARY' in df.columns:
        df['_r_subsidiary_flag'] = df['URL_IS_SUBSIDIARY'] == 1
    elif 'Is_Subsidiary' in df.columns:
        df['_r_subsidiary_flag'] = df['Is_Subsidiary'].astype(str).str.strip().str.lower().isin(['1', 'true', 'yes'])
    else:
        df['_r_subsidiary_flag'] = False
    # Subsidiary flag is only valid when the URL is NOT on a known third-party site
    # (generic/registry/aggregator checks already computed above)
    df['_r_subsidiary'] = (
        df['_r_subsidiary_flag'] &
        ~df['_r_generic'] &
        ~df['_r_registry'] &
        ~df['_r_generic_host']
    )
    if df['_r_subsidiary_flag'].any():
        n_flag = df['_r_subsidiary_flag'].sum()
        n_valid = df['_r_subsidiary'].sum()
        n_blocked = n_flag - n_valid
        log.info(f"Subsidiary flag: {n_flag} flagged, {n_valid} valid (URL not third-party), "
                 f"{n_blocked} blocked (URL on third-party site)")

    # Rule 5: Company URL match (if company_url_col provided)
    #   Compare RELEVANT_URL domain against the issuer's own official URL.
    #   Two checks: (a) stem match, (b) subdomain match (URL domain ends with company domain)
    if company_url_col and company_url_col in df.columns:
        def _extract_company_stem(val):
            if pd.isna(val):
                return None
            val = str(val).strip()
            # If bare domain (no protocol), prepend https://
            if val and not val.startswith(('http://', 'https://')):
                val = 'https://' + val
            return extract_domain_stem(val)

        def _extract_company_root(val):
            """Extract the full root domain (e.g., bangchak.co.th) for subdomain matching."""
            if pd.isna(val):
                return None
            val = str(val).strip().rstrip('/')
            if not val.startswith(('http://', 'https://')):
                val = 'https://' + val
            return extract_root_domain(val)

        df['_company_stem'] = df[company_url_col].apply(_extract_company_stem)
        df['_company_root'] = df[company_url_col].apply(_extract_company_root)
        df['_url_root'] = df['RELEVANT_URL'].apply(extract_root_domain)

        # (a) Stem match: domain stems are identical
        stem_match = (
            df['domain_stem'].notna() &
            df['_company_stem'].notna() &
            (df['domain_stem'] == df['_company_stem'])
        )
        # (b) Subdomain match: URL domain ends with company's root domain
        #     e.g., bcplineoa.bangchak.co.th ends with bangchak.co.th
        def _is_subdomain(url_root, co_root):
            if pd.isna(url_root) or pd.isna(co_root) or not url_root or not co_root:
                return False
            return url_root == co_root or url_root.endswith('.' + co_root)

        subdomain_match = df.apply(
            lambda r: _is_subdomain(r.get('_url_root', ''), r.get('_company_root', '')), axis=1
        )

        df['_r_company_url'] = stem_match | subdomain_match
        n_stem = stem_match.sum()
        n_sub = (subdomain_match & ~stem_match).sum()
        log.info(f"Company URL match ({company_url_col}): {df['_r_company_url'].sum()} rows "
                 f"({n_stem} stem, {n_sub} subdomain)")
    else:
        df['_r_company_url'] = False

    # Rule 6: Upstream suggestion columns (flagged mode — URL_CONSISTENCY_FLAG==1)
    # When the upstream system already suggests a different issuer for this URL,
    # route to Manual Review with the suggestion info.
    _suggestion_col = 'URL_CONSISTENCY_SUGGESTED_ISSUER_ID'
    _has_upstream = _suggestion_col in df.columns
    if _has_upstream:
        df['_r_upstream_suggestion'] = df[_suggestion_col].notna()
        n_upstream = df['_r_upstream_suggestion'].sum()
        log.info(f"Upstream suggestions found: {n_upstream} rows have suggested issuer IDs")
    else:
        df['_r_upstream_suggestion'] = False

    # Rule 7: Majority domain within same issuer
    # NOTE: Majority domain alone is NOT sufficient for auto-classification.
    # A third-party site (e.g. xueqiu.com for SIHL) can be the majority domain
    # if data was primarily collected from that source.
    # We only auto-classify if the domain stem also has a textual link to the issuer name.
    majority = df.groupby('ISSUER_ID')['domain_stem'].agg(
        lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else None
    )
    df['_majority_stem'] = df['ISSUER_ID'].map(majority)
    df['_r_majority'] = df['domain_stem'] == df['_majority_stem']

    # Check if the majority domain stem has a textual relationship to the issuer name
    def _majority_name_match(row):
        """
        Return True only if the majority domain stem is textually linked to the
        issuer name — i.e. the stem appears in the name or vice versa.
        Without this link, the majority domain could be a third-party aggregator
        that happens to have many URLs for this issuer.
        """
        if not row['_r_majority']:
            return False
        stem = row.get('domain_stem', '')
        name = str(row.get('ISSUER_NAME', '')).lower()
        if pd.isna(stem) or not stem or not name:
            return False
        # Normalize issuer name: remove punctuation, common suffixes
        import re as _re
        name_clean = _re.sub(r'[^a-z0-9\s]', '', name)
        name_tokens = [t for t in name_clean.split() if len(t) > 2]
        # Check: stem in name or first significant name token in stem
        if stem in name_clean.replace(' ', ''):
            return True
        for token in name_tokens[:3]:  # Check first 3 tokens
            if len(token) > 3 and token in stem:
                return True
            if len(stem) > 3 and stem in token:
                return True
        return False

    df['_r_majority_verified'] = df.apply(_majority_name_match, axis=1)

    # Combine into a single decision (priority order matters)
    # CRITICAL: Registry/exchange/aggregator check runs FIRST and overrides everything.
    # A Companies House page about a subsidiary is still a third-party registry page.
    def _decide(row):
        if row['_r_generic']:
            return 'AUTO_URLC02_GENERIC'
        if row['_r_registry']:
            return 'AUTO_URLC02_REGISTRY'
        if row['_r_generic_host']:
            return 'AUTO_URLC02_GENERIC_HOST'
        if row['_r_report']:
            return 'AUTO_URLC01_REPORT_HOST'
        if row['_r_child'] == 'PARENT_STEM_MATCH':
            return 'AUTO_URLC01_PARENT_URL'
        if row['_r_child'] in ('CHILD_STEM_MATCH', 'NAME_PARTIAL'):
            return 'MANUAL_REVIEW_CHILD_TABLE'
        if row['_r_upstream_suggestion']:
            return 'MANUAL_REVIEW_UPSTREAM_SUGGESTION'
        if row['_r_subsidiary'] and row['_r_company_url']:
            # Subsidiary flag + URL is on the company's own domain → safe URLC01
            return 'AUTO_URLC01_SUBSIDIARY'
        if row['_r_company_url']:
            return 'AUTO_URLC01_COMPANY_URL'
        if row['_r_subsidiary']:
            # Subsidiary flag but URL is NOT on company domain → needs review
            # The URL may be about a subsidiary but hosted on a third-party site
            return 'MANUAL_REVIEW_SUBSIDIARY'
        # Majority domain: only auto-classify if name-verified
        if row['_r_majority_verified']:
            return 'AUTO_URLC01_MAJORITY_DOMAIN'
        # Majority domain WITHOUT name match → needs LLM verification
        if row['_r_majority']:
            return 'MAJORITY_NEEDS_LLM_REVIEW'
        return 'NEEDS_LLM_REVIEW'

    df['auto_decision'] = df.apply(_decide, axis=1)

    # Log majority domain split
    majority_verified = (df['auto_decision'] == 'AUTO_URLC01_MAJORITY_DOMAIN').sum()
    majority_unverified = (df['auto_decision'] == 'MAJORITY_NEEDS_LLM_REVIEW').sum()
    if majority_verified + majority_unverified > 0:
        log.info(f"Majority domain: {majority_verified} name-verified (auto URLC01), "
                 f"{majority_unverified} unverified (→ LLM review)")

    # ── Populate suggestion output columns ──────────────────────────────────
    # Initialize the 4 standard output columns
    for col in ['URL_CONSISTENCY_SUGGESTED_ISSUER_ID', 'URL_CONSISTENCY_SUGGESTED_ISSUER_NAME',
                'URL_CONSISTENCY_SUGGESTION_URL', 'URL_CONSISTENCY_SUGGESTION_BASIS']:
        if col not in df.columns:
            df[col] = None

    # Fill from child table matches
    child_mask = df['auto_decision'] == 'MANUAL_REVIEW_CHILD_TABLE'
    if child_mask.any():
        df.loc[child_mask, 'URL_CONSISTENCY_SUGGESTED_ISSUER_ID'] = df.loc[child_mask, '_matched_child_issuer_id']
        df.loc[child_mask, 'URL_CONSISTENCY_SUGGESTED_ISSUER_NAME'] = df.loc[child_mask, '_matched_child_name']
        df.loc[child_mask, 'URL_CONSISTENCY_SUGGESTION_URL'] = df.loc[child_mask, '_matched_child_url']
        df.loc[child_mask, 'URL_CONSISTENCY_SUGGESTION_BASIS'] = df.loc[child_mask, '_r_child'].apply(
            lambda x: 'CHILD_TABLE_' + x if x else 'CHILD_TABLE')
        log.info(f"Populated suggestion columns for {child_mask.sum()} child table matches")

    # For upstream suggestion rows, the columns already exist from the input — preserve them
    upstream_mask = df['auto_decision'] == 'MANUAL_REVIEW_UPSTREAM_SUGGESTION'
    if upstream_mask.any():
        log.info(f"Upstream suggestion rows preserved: {upstream_mask.sum()}")

    # Clean up temp columns
    _temp_prefixes = ('_r_', '_majority', '_matched_child', '_company_stem',
                      '_company_root', '_url_root')
    df.drop(columns=[c for c in df.columns if any(c.startswith(p) for p in _temp_prefixes)],
            inplace=True)

    return df


# =============================================================================
#  LAYER 3 — LLM VERIFICATION (issuer+domain combo level)
# =============================================================================

def get_llm_review_combos(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse NEEDS_LLM_REVIEW rows into unique (ISSUER_ID, ISSUER_NAME, domain_stem)
    combos. Each combo needs only one LLM call; the result propagates to all matching rows.

    Returns DataFrame with columns:
      ISSUER_ID, ISSUER_NAME, domain_stem, row_count, sample_facilities, sample_urls, url_flag
    """
    review = df[df['auto_decision'].isin(['NEEDS_LLM_REVIEW', 'MAJORITY_NEEDS_LLM_REVIEW'])].copy()
    if review.empty:
        return pd.DataFrame()

    # Build aggregation dict — handle optional columns gracefully
    agg_dict = {
        'row_count': ('RELEVANT_URL', 'size'),
        'sample_urls': ('RELEVANT_URL', lambda x: list(x.head(2))),
    }
    if 'FACILITY_NAME' in review.columns:
        agg_dict['sample_facilities'] = ('FACILITY_NAME', lambda x: list(x.head(3)))
    if 'URL_CONSISTENCY_FLAG' in review.columns:
        agg_dict['url_flag'] = ('URL_CONSISTENCY_FLAG', 'first')

    combos = (
        review.groupby(['ISSUER_ID', 'ISSUER_NAME', 'domain_stem'])
        .agg(**agg_dict)
        .reset_index()
        .sort_values('row_count', ascending=False)
    )

    # Ensure expected columns exist (with defaults if source columns were missing)
    if 'sample_facilities' not in combos.columns:
        combos['sample_facilities'] = [[] for _ in range(len(combos))]
    if 'url_flag' not in combos.columns:
        combos['url_flag'] = ''

    return combos


def build_llm_prompt(issuer_name: str, domain_stem: str, sample_urls: list,
                     sample_facilities: list, url_flag: str,
                     search_evidence: str = "") -> str:
    """
    Build a structured prompt for LLM verification of one issuer+domain combo.

    Can be sent to Claude API, OpenAI, or any LLM endpoint.
    """
    prompt = f"""You are a corporate research analyst verifying URL ownership for ESG data quality.

TASK: Determine whether the domain "{domain_stem}" legitimately belongs to or is associated with the company "{issuer_name}".

CONTEXT:
- Issuer (parent company): {issuer_name}
- Domain stem in question: {domain_stem}
- System flag reason: {url_flag}
- Sample facility names using this URL: {json.dumps(sample_facilities[:3])}
- Sample URLs: {json.dumps(sample_urls[:2])}

VERIFICATION CRITERIA — mark as VALID (URLC01) if ANY of these apply:
1. The domain belongs to a known subsidiary, brand, or operating company of the issuer
2. The domain is an abbreviation, acronym, or trade name of the issuer
3. The domain is a country-specific variant of the issuer's main website
4. The domain hosts official company filings, annual reports, or investor relations content
5. The facility names in the data clearly relate to the business described on the domain
6. The issuer has a majority ownership stake (>50%) in the entity that owns the domain

Mark as INVALID (URLC02) if:
1. The domain belongs to a completely different, unrelated company
2. The domain is a generic third-party site (Google Maps, LinkedIn, etc.)
3. The URL was clearly assigned to the wrong issuer (data entry error)
4. The domain hosts content for a competitor or unrelated entity
5. The domain belongs to an independent distributor/dealer (not owned by the issuer)
6. The domain is a generic hosting platform (SiteGround, Heroku, etc.) not owned by the company

RESPOND with exactly one JSON object and nothing else — no markdown, no code fences, no explanation outside the JSON:
{{"verdict": "URLC01" or "URLC02", "confidence": "HIGH" or "MEDIUM" or "LOW", "reason": "<one-line explanation>"}}"""

    if search_evidence:
        prompt += f"\n\nWEB SEARCH EVIDENCE:\n{search_evidence}"

    return prompt


def parse_llm_json_response(response: str) -> dict:
    """
    Parse an LLM verdict response into a dict.

    Tolerates markdown fences, leading/trailing prose, and nested braces.
    """
    if response is None:
        raise ValueError("empty LLM response")
    text = str(response).strip()
    if not text:
        raise ValueError("empty LLM response")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        parsed = json.loads(fenced.group(1))
        if isinstance(parsed, dict):
            return parsed

    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    raise ValueError(f"no JSON object found in LLM response: {text[:200]!r}")


def build_search_queries(issuer_name: str, domain_stem: str) -> list:
    """Build web search queries to gather evidence for LLM verification."""
    return [
        f'"{domain_stem}" "{issuer_name}" subsidiary OR brand OR company',
        f'{domain_stem} company owner parent',
    ]


def verify_combo_with_search(
    issuer_name: str,
    domain_stem: str,
    sample_urls: list,
    sample_facilities: list,
    url_flag: str,
    search_fn: Optional[Callable] = None,
    llm_fn: Optional[Callable] = None,
) -> dict:
    """
    Verify a single issuer+domain combo using web search + LLM reasoning.

    Parameters
    ----------
    search_fn : callable(query: str) -> str, optional
        Web search function. If None, no search evidence is gathered.
    llm_fn : callable(prompt: str) -> str, optional
        LLM completion function. If None, returns the prompt for external processing.

    Returns
    -------
    dict with keys: verdict, confidence, reason, prompt, search_evidence
    """
    # Gather search evidence
    search_evidence = ""
    if search_fn:
        for q in build_search_queries(issuer_name, domain_stem):
            try:
                result = search_fn(q)
                search_evidence += f"\n--- Search: {q} ---\n{result}\n"
            except Exception as e:
                log.warning(f"Search failed for '{q}': {e}")

    # Build prompt
    prompt = build_llm_prompt(
        issuer_name, domain_stem, sample_urls, sample_facilities,
        url_flag, search_evidence
    )

    result = {
        'issuer_name': issuer_name,
        'domain_stem': domain_stem,
        'prompt': prompt,
        'search_evidence': search_evidence,
        'verdict': None,
        'confidence': None,
        'reason': None,
    }

    # Call LLM if available
    if llm_fn:
        try:
            response = llm_fn(prompt)
            if not response or not str(response).strip():
                raise ValueError("empty LLM response")
            parsed = parse_llm_json_response(response)
            result['verdict'] = parsed.get('verdict')
            result['confidence'] = parsed.get('confidence')
            result['reason'] = parsed.get('reason')
            if not result['verdict']:
                raise ValueError(f"missing verdict in parsed JSON: {parsed!r}")
        except Exception as e:
            log.warning(f"LLM call failed for {issuer_name}/{domain_stem}: {e}")
            result['reason'] = f"LLM_ERROR: {e}"

    return result


def batch_verify_combos(
    combos_df: pd.DataFrame,
    search_fn: Optional[Callable] = None,
    llm_fn: Optional[Callable] = None,
) -> list:
    """
    Run LLM verification on all combos from get_llm_review_combos().

    Returns list of verdict dicts.
    """
    results = []
    total = len(combos_df)
    for i, (_, row) in enumerate(combos_df.iterrows()):
        log.info(f"Verifying combo {i+1}/{total}: {row['ISSUER_NAME']} / {row['domain_stem']} ({row['row_count']} rows)")
        result = verify_combo_with_search(
            issuer_name=row['ISSUER_NAME'],
            domain_stem=row['domain_stem'],
            sample_urls=row['sample_urls'],
            sample_facilities=row['sample_facilities'],
            url_flag=row['url_flag'],
            search_fn=search_fn,
            llm_fn=llm_fn,
        )
        result['ISSUER_ID'] = row['ISSUER_ID']
        result['row_count'] = row['row_count']
        results.append(result)
    return results


def verdicts_to_dict(results: list) -> dict:
    """Convert batch_verify_combos output to a dict for run_pipeline(llm_verdicts=...)."""
    return {
        (r['ISSUER_ID'], r['domain_stem']): r['verdict']
        for r in results
        if r.get('verdict')
    }


# =============================================================================
#  FULL PIPELINE
# =============================================================================

def run_pipeline(
    main_df: pd.DataFrame,
    child_df: pd.DataFrame,
    llm_verdicts: Optional[dict] = None,
    company_url_col: Optional[str] = None,
) -> pd.DataFrame:
    """
    Run the full URL consistency classification pipeline.

    Parameters
    ----------
    main_df : DataFrame
        The flagged URL data (rows with URL consistency issues).
    child_df : DataFrame
        The issuer-child relationship table.
    llm_verdicts : dict, optional
        Pre-computed LLM verdicts keyed by (ISSUER_ID, domain_stem) → 'URLC01'/'URLC02'.
        If None, NEEDS_LLM_REVIEW rows stay unresolved.
    company_url_col : str, optional
        Column name containing the company's official URL. If provided,
        domain stems are compared to classify matches as URLC01.

    Returns
    -------
    DataFrame with columns added:
      - domain_stem: extracted domain stem
      - auto_decision: classification rule/source
      - predicted_url_code: final URLC01/URLC02/UNRESOLVED
    """
    log.info("Building child table lookup...")
    child_lookup = build_child_lookup(child_df)
    log.info(f"Child lookup built for {len(child_lookup)} issuers")

    log.info("Applying deterministic rules (Layer 1 + 2)...")
    result = apply_deterministic_rules(main_df, child_lookup, company_url_col=company_url_col)

    # Summary
    counts = result['auto_decision'].value_counts()
    log.info(f"Deterministic rule results:\n{counts.to_string()}")

    # Apply LLM verdicts if provided
    if llm_verdicts:
        review_decisions = {'NEEDS_LLM_REVIEW', 'MAJORITY_NEEDS_LLM_REVIEW'}
        log.info(f"Applying {len(llm_verdicts)} LLM verdicts to review rows...")

        def _apply_llm(row):
            if row['auto_decision'] not in review_decisions:
                return row['auto_decision']
            key = (row['ISSUER_ID'], row['domain_stem'])
            verdict = llm_verdicts.get(key)
            if verdict:
                # Preserve the source info: was this a majority-domain combo?
                prefix = "LLM_MAJ_" if row['auto_decision'] == 'MAJORITY_NEEDS_LLM_REVIEW' else "LLM_"
                return f"{prefix}{verdict}"
            return row['auto_decision']  # Keep original tag if no verdict

        result['auto_decision'] = result.apply(_apply_llm, axis=1)
        log.info(f"After LLM:\n{result['auto_decision'].value_counts().to_string()}")

    # Map auto_decision → final URLC code
    code_map = {
        'AUTO_URLC01_REPORT_HOST': 'URLC01',
        'AUTO_URLC01_PARENT_URL': 'URLC01',
        'MANUAL_REVIEW_CHILD_TABLE': 'MANUAL_REVIEW',
        'MANUAL_REVIEW_UPSTREAM_SUGGESTION': 'MANUAL_REVIEW',
        'MANUAL_REVIEW_SUBSIDIARY': 'MANUAL_REVIEW',
        'AUTO_URLC01_SUBSIDIARY': 'URLC01',
        'AUTO_URLC01_COMPANY_URL': 'URLC01',
        'AUTO_URLC01_MAJORITY_DOMAIN': 'URLC01',
        'AUTO_URLC02_GENERIC': 'URLC02',
        'AUTO_URLC02_REGISTRY': 'URLC02',
        'AUTO_URLC02_GENERIC_HOST': 'URLC02',
        'LLM_URLC01': 'URLC01',
        'LLM_URLC02': 'URLC02',
        'LLM_MAJ_URLC01': 'URLC01',
        'LLM_MAJ_URLC02': 'URLC02',
    }
    result['predicted_url_code'] = result['auto_decision'].map(code_map).fillna('UNRESOLVED')

    resolved = (result['predicted_url_code'] != 'UNRESOLVED').sum()
    total = len(result)
    log.info(f"Final: Resolved {resolved}/{total} rows ({resolved/total*100:.1f}%)")

    return result


# =============================================================================
#  EVALUATION HELPERS
# =============================================================================

def evaluate_against_ground_truth(result_df: pd.DataFrame, gt_col: str = 'COMMENT_CODE') -> dict:
    """
    Compare predicted_url_code against ground truth COMMENT_CODE.

    Returns dict with accuracy metrics per rule and overall.
    """
    df = result_df.copy()
    df['gt_code'] = df[gt_col].apply(
        lambda x: 'URLC01' if 'URLC01' in str(x) else ('URLC02' if 'URLC02' in str(x) else 'OTHER')
    )

    resolved = df[df['predicted_url_code'] != 'UNRESOLVED']

    metrics = {
        'total_rows': len(df),
        'resolved_rows': len(resolved),
        'resolution_rate': len(resolved) / len(df) * 100,
        'correct': (resolved['predicted_url_code'] == resolved['gt_code']).sum(),
        'incorrect': (resolved['predicted_url_code'] != resolved['gt_code']).sum(),
    }
    metrics['accuracy'] = metrics['correct'] / metrics['resolved_rows'] * 100 if metrics['resolved_rows'] > 0 else 0

    # Per-rule breakdown
    per_rule = {}
    for rule, grp in resolved.groupby('auto_decision'):
        correct = (grp['predicted_url_code'] == grp['gt_code']).sum()
        per_rule[rule] = {
            'rows': len(grp),
            'correct': correct,
            'incorrect': len(grp) - correct,
            'accuracy': correct / len(grp) * 100,
        }
    metrics['per_rule'] = per_rule

    return metrics
