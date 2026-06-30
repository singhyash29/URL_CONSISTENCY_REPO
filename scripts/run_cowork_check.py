"""
URL Consistency Check — COWORK MODE (Claude-in-session as the LLM)
===================================================================
Designed for running inside Cowork where Claude IS the LLM.
Instead of calling external APIs (OpenAI/Azure/Anthropic), this script:

  1. Runs deterministic rules + cached verdicts (Layers 1+2)
  2. Exports unresolved combos to a structured JSON file
  3. Claude in Cowork reads the combos, classifies them using its own knowledge
  4. Claude writes verdicts back to the cache
  5. Re-runs the pipeline to produce the final output

Workflow (run from Cowork):
  Step 1 — First pass (deterministic + cache):
    python scripts/run_cowork_check.py --input input/data.xlsx --step 1

  Step 2 — Claude classifies unresolved combos (done by Claude in Cowork)
    → Claude reads output/cowork_unresolved.json
    → Claude writes verdicts to cache/llm_verdicts.json

  Step 3 — Final pass (re-run with updated cache):
    python scripts/run_cowork_check.py --input input/data.xlsx --step 3

  Or run all-in-one (Steps 1+3, skipping unresolved):
    python scripts/run_cowork_check.py --input input/data.xlsx

Supports both --mode full and --mode flagged.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_common import (
    load_dotenv,
    load_input,
    resolve_path,
    load_llm_cache,
    load_llm_reasons,
    save_llm_cache,
    save_llm_reasons,
    run_classification,
    PROJECT_ROOT,
    INPUT_DIR,
    OUTPUT_DIR,
    CACHE_DIR,
    CHILD_TABLE_PATH,
    log,
)

load_dotenv()

UNRESOLVED_PATH = OUTPUT_DIR / "cowork_unresolved.json"


def step1_deterministic(args, fail_df, pass_df):
    """Run deterministic rules + cache. Export unresolved combos for Claude."""
    from url_consistency_engine import run_pipeline, get_llm_review_combos

    child_path = resolve_path(args.child_table, PROJECT_ROOT)
    log.info(f"Loading child table from {child_path}")
    child_df = __import__('pandas').read_excel(str(child_path))
    log.info(f"Loaded {len(child_df)} child records")

    llm_verdicts = load_llm_cache()
    company_url_col = args.company_url_col
    if company_url_col and company_url_col not in fail_df.columns:
        log.warning(f"Company URL column '{company_url_col}' not found. Skipping.")
        company_url_col = None

    result = run_pipeline(fail_df, child_df,
                          llm_verdicts=llm_verdicts if llm_verdicts else None,
                          company_url_col=company_url_col)

    combos = get_llm_review_combos(result)

    if combos.empty:
        log.info("No unresolved combos — all rows classified by rules + cache!")
        return 0

    # Export unresolved combos as structured JSON for Claude to read
    combo_list = []
    for _, row in combos.iterrows():
        combo_list.append({
            "issuer_id": str(row["ISSUER_ID"]),
            "issuer_name": str(row["ISSUER_NAME"]),
            "domain_stem": str(row["domain_stem"]),
            "row_count": int(row["row_count"]),
            "sample_urls": row.get("sample_urls", []),
            "sample_facilities": row.get("sample_facilities", []),
        })

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(UNRESOLVED_PATH, "w") as f:
        json.dump(combo_list, f, indent=2, default=str)

    log.info(f"\n{'='*60}")
    log.info(f"  STEP 1 COMPLETE — {len(combo_list)} unresolved combos exported")
    log.info(f"  File: {UNRESOLVED_PATH}")
    log.info(f"")
    log.info(f"  Next: Claude in Cowork should read this file, classify")
    log.info(f"  each combo as URLC01 or URLC02, and write verdicts to")
    log.info(f"  cache/llm_verdicts.json. Then run --step 3.")
    log.info(f"{'='*60}")

    return len(combo_list)


def write_verdicts_to_cache(verdicts: list):
    """
    Write Claude's verdicts to the LLM cache.

    Parameters
    ----------
    verdicts : list of dict
        Each dict has: issuer_id, domain_stem, verdict ('URLC01'/'URLC02'), reason
    """
    cache = load_llm_cache()
    reasons = load_llm_reasons()

    added = 0
    for v in verdicts:
        key = (str(v["issuer_id"]), str(v["domain_stem"]))
        if key not in cache:
            added += 1
        cache[key] = v["verdict"]
        if v.get("reason"):
            reason_key = f"{v.get('issuer_name', v['issuer_id'])}|||{v['domain_stem']}"
            reasons[reason_key] = v["reason"]

    save_llm_cache(cache)
    save_llm_reasons(reasons)
    log.info(f"Wrote {added} new verdicts to cache (total: {len(cache)})")
    return added


def step3_final(args, fail_df, pass_df, output_path):
    """Re-run pipeline with updated cache and produce final output."""
    summary = run_classification(
        fail_df=fail_df,
        pass_df=pass_df,
        child_path=str(resolve_path(args.child_table, PROJECT_ROOT)),
        company_url_col=args.company_url_col,
        skip_llm=True,       # Always skip external LLM — we use cache only
        provider=None,
        model=None,
        output_path=str(output_path),
        run_eval=args.eval,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="URL Consistency Check — COWORK MODE (Claude as LLM)"
    )
    parser.add_argument("--input", required=True,
                        help="Path to input file (.pkl, .xlsx, .csv)")
    parser.add_argument("--mode", choices=["full", "flagged"], default="full",
                        help="Check mode: 'full' = all rows (default), 'flagged' = URL_CONSISTENCY_CHECK==1 only")
    parser.add_argument("--step", choices=["1", "3", "all"], default="all",
                        help="Pipeline step: '1' = deterministic + export unresolved, "
                             "'3' = final pass with cache, 'all' = 1+3 (skip unresolved)")
    parser.add_argument("--output", default=None,
                        help="Output Excel path")
    parser.add_argument("--child-table", default=str(CHILD_TABLE_PATH),
                        help="Path to issuer_child_flagged.xlsx")
    parser.add_argument("--company-url-col", default=None,
                        help="Column containing the company's official URL")
    parser.add_argument("--eval", action="store_true",
                        help="Run evaluation against ground truth")
    args = parser.parse_args()

    # ── Resolve paths ────────────────────────────────────────────────────────
    input_path = resolve_path(args.input, PROJECT_ROOT)

    if args.output:
        output_path = resolve_path(args.output, PROJECT_ROOT)
    else:
        stem = input_path.stem
        output_path = OUTPUT_DIR / f"{stem}_cowork_results.xlsx"

    # ── Load data ────────────────────────────────────────────────────────────
    log.info(f"Loading input data from {input_path}")
    full_df = load_input(str(input_path))
    log.info(f"Loaded {len(full_df)} total rows")

    # ── Determine which rows to classify ─────────────────────────────────────
    if args.mode == "full":
        fail_df = full_df.copy()
        pass_df = None
        log.info(f"MODE=full: all {len(fail_df)} rows")
    else:
        if "URL_CONSISTENCY_CHECK" not in full_df.columns:
            log.warning("URL_CONSISTENCY_CHECK column not found — treating all rows as flagged.")
            fail_df = full_df.copy()
            pass_df = None
        else:
            mask = full_df["URL_CONSISTENCY_CHECK"] == 1
            fail_df = full_df[mask].copy()
            pass_df = full_df[~mask].copy()
            log.info(f"MODE=flagged: {len(fail_df)} flagged, {len(pass_df)} passing")

    if fail_df.empty:
        log.info("No rows to classify.")
        return

    # ── Execute the requested step ───────────────────────────────────────────
    if args.step == "1":
        step1_deterministic(args, fail_df, pass_df)

    elif args.step == "3":
        step3_final(args, fail_df, pass_df, output_path)

    else:  # "all" — run deterministic + final (no Claude-in-the-middle)
        n_unresolved = step1_deterministic(args, fail_df, pass_df)
        if n_unresolved > 0:
            log.info(f"\n  {n_unresolved} combos unresolved — running final pass with cache as-is.")
            log.info(f"  To improve: run --step 1, classify combos, then --step 3.\n")
        step3_final(args, fail_df, pass_df, output_path)


if __name__ == "__main__":
    main()
