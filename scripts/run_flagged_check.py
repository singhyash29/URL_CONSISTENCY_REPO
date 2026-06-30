"""
URL Consistency Check — FLAGGED CHECK (URL_CONSISTENCY_CHECK==1 only)
======================================================================
Runs the classification pipeline ONLY on rows where
URL_CONSISTENCY_CHECK == 1.  Passing rows (==0) are preserved in the
output file but receive no verdict.

Use this when you only want to classify the rows that the upstream
consistency check has already flagged as failures.

Usage:
  # Basic (deterministic rules + cached LLM verdicts)
  python scripts/run_flagged_check.py --input input/your_data.xlsx

  # With company URL column for domain matching
  python scripts/run_flagged_check.py --input input/your_data.xlsx --company-url-col COMPANY_URL

  # With live LLM verification
  python scripts/run_flagged_check.py --input input/your_data.xlsx --provider anthropic

  # Skip LLM entirely (cache + rules only)
  python scripts/run_flagged_check.py --input input/your_data.xlsx --skip-llm

Folders:
  input/    — Place your data + issuer_child_flagged.xlsx here
  output/   — Results written here
  cache/    — LLM verdict cache (reused across runs)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_common import (
    load_dotenv,
    load_input,
    resolve_path,
    run_classification,
    PROJECT_ROOT,
    INPUT_DIR,
    OUTPUT_DIR,
    CHILD_TABLE_PATH,
    log,
)

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="URL Consistency Check — FLAGGED CHECK: classify only URL_CONSISTENCY_CHECK==1 rows"
    )
    parser.add_argument("--input", required=True,
                        help="Path to input file (.pkl, .xlsx, .csv)")
    parser.add_argument("--output", default=None,
                        help="Output Excel path (default: output/<input_name>_flagged_results.xlsx)")
    parser.add_argument("--child-table", default=str(CHILD_TABLE_PATH),
                        help="Path to issuer_child_flagged.xlsx")
    parser.add_argument("--company-url-col", default=None,
                        help="Column containing the company's official URL for domain matching")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Skip LLM verification, use cache + rules only")
    parser.add_argument("--provider", choices=["anthropic", "vertex", "openai", "azure"], default=None,
                        help="LLM provider for verifying new combos")
    parser.add_argument("--model", default=None,
                        help="LLM model / Azure deployment override")
    parser.add_argument("--eval", action="store_true",
                        help="Run evaluation against ground truth (needs COMMENT_CODE column)")
    parser.add_argument("--verdict-mode", choices=["v1", "v2", "v3", "v4", "v5"], default="v1",
                        help="Verdict mode: v1=URLC01+ManualReview, v2=URLC01+URLC02+URLC03+ManualReview, v3=FLAG==1, v4=strict company+Tag, v5=v4+allowlist→URLC03+Tag")
    args = parser.parse_args()

    # ── Resolve paths ────────────────────────────────────────────────────────
    input_path = resolve_path(args.input, PROJECT_ROOT)

    if args.output:
        output_path = resolve_path(args.output, PROJECT_ROOT)
    else:
        stem = input_path.stem
        vm = args.verdict_mode
        output_path = OUTPUT_DIR / f"{stem}_{vm}_flagged_results.xlsx"

    child_path = resolve_path(args.child_table, PROJECT_ROOT)

    # ── Load data ────────────────────────────────────────────────────────────
    log.info(f"Loading input data from {input_path}")
    full_df = load_input(str(input_path))
    log.info(f"Loaded {len(full_df)} total rows")

    # ── FLAGGED CHECK: filter for URL_CONSISTENCY_CHECK == 1 ─────────────────
    if "URL_CONSISTENCY_CHECK" not in full_df.columns:
        log.error(
            "Column 'URL_CONSISTENCY_CHECK' not found in input file.\n"
            "This script requires the column to filter flagged rows.\n"
            "If you want to run on ALL rows, use:  run_full_check.py"
        )
        sys.exit(1)

    fail_mask = full_df["URL_CONSISTENCY_CHECK"] == 1
    fail_df = full_df[fail_mask].copy()
    pass_df = full_df[~fail_mask].copy()

    log.info(
        f"FLAGGED CHECK mode: {len(fail_df)} flagged rows (URL_CONSISTENCY_CHECK==1), "
        f"{len(pass_df)} passing rows skipped"
    )

    if fail_df.empty:
        log.info("No flagged rows to classify. Nothing to do.")
        return

    # ── Run classification ───────────────────────────────────────────────────
    summary = run_classification(
        fail_df=fail_df,
        pass_df=pass_df,       # Passing rows merged back with no verdict
        child_path=str(child_path),
        company_url_col=args.company_url_col,
        skip_llm=args.skip_llm,
        provider=args.provider,
        model=args.model,
        output_path=str(output_path),
        run_eval=args.eval,
        verdict_mode=args.verdict_mode,
    )

    log.info("FLAGGED CHECK complete.")


if __name__ == "__main__":
    main()
