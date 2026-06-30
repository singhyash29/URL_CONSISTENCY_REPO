"""
URL Consistency Check — FULL CHECK (all rows)
===============================================
Runs the classification pipeline on EVERY row in the input file,
regardless of the URL_CONSISTENCY_CHECK column value.

Use this when you want to classify all URLs in the dataset — not just
those flagged by the upstream consistency check.

Usage:
  # Basic (deterministic rules + cached LLM verdicts)
  python scripts/run_full_check.py --input input/your_data.xlsx

  # With company URL column for domain matching
  python scripts/run_full_check.py --input input/your_data.xlsx --company-url-col COMPANY_URL

  # With live LLM verification
  python scripts/run_full_check.py --input input/your_data.xlsx --provider anthropic

  # Skip LLM entirely (cache + rules only)
  python scripts/run_full_check.py --input input/your_data.xlsx --skip-llm

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
        description="URL Consistency Check — FULL CHECK: classify ALL rows in the input file"
    )
    parser.add_argument("--input", required=True,
                        help="Path to input file (.pkl, .xlsx, .csv)")
    parser.add_argument("--output", default=None,
                        help="Output Excel path (default: output/<input_name>_full_results.xlsx)")
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
        output_path = OUTPUT_DIR / f"{stem}_{vm}_full_results.xlsx"

    child_path = resolve_path(args.child_table, PROJECT_ROOT)

    # ── Load data ────────────────────────────────────────────────────────────
    log.info(f"Loading input data from {input_path}")
    full_df = load_input(str(input_path))
    log.info(f"Loaded {len(full_df)} total rows")

    # ── FULL CHECK: use ALL rows ─────────────────────────────────────────────
    log.info(f"FULL CHECK mode: Running pipeline on all {len(full_df)} rows")

    summary = run_classification(
        fail_df=full_df,
        pass_df=None,          # No rows skipped — every row gets classified
        child_path=str(child_path),
        company_url_col=args.company_url_col,
        skip_llm=args.skip_llm,
        provider=args.provider,
        model=args.model,
        output_path=str(output_path),
        run_eval=args.eval,
        verdict_mode=args.verdict_mode,
    )

    log.info("FULL CHECK complete.")


if __name__ == "__main__":
    main()
