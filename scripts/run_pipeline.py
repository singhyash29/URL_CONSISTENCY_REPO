"""
URL Consistency Check — Unified Pipeline Runner
=================================================
Single entry point that dispatches to either FULL or FLAGGED check mode.

Modes:
  --mode full     → Classify ALL rows (same as run_full_check.py)
  --mode flagged  → Classify only URL_CONSISTENCY_CHECK==1 rows (default)

Legacy flag --all-rows is still supported as a shortcut for --mode full.

Usage:
  # Flagged check (default) — only URL_CONSISTENCY_CHECK==1 rows
  python scripts/run_pipeline.py --input input/your_data.xlsx

  # Full check — all rows
  python scripts/run_pipeline.py --mode full --input input/your_data.xlsx

  # Full check (legacy syntax, same as above)
  python scripts/run_pipeline.py --input input/your_data.xlsx --all-rows

  # With company URL column + skip LLM
  python scripts/run_pipeline.py --mode full --input input/data.xlsx --company-url-col COMPANY_URL --skip-llm

  # With live LLM verification
  python scripts/run_pipeline.py --input input/data.xlsx --provider anthropic

  # Verdict modes
  python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v1   # URLC01 & Manual Review
  python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v2   # URLC01, URLC02, URLC03, Manual Review
  python scripts/run_pipeline.py --input input/data.xlsx --verdict-mode v3   # URLC01 & Manual Review (FLAG==1)

API Keys (set via environment variable or .env file in project root):
  Anthropic:  ANTHROPIC_API_KEY=sk-ant-...
  OpenAI:     OPENAI_API_KEY=sk-...
  Azure:      AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT

Folders:
  input/    — Place your data + issuer_child_flagged.xlsx here
  output/   — Results written here (original file + verdict & explanation columns)
  cache/    — LLM verdict cache (reused across runs)
  scripts/  — Engine module + runners
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

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
    rows_to_skip_mask,
    SKIP_VERDICT,
    log,
)

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="URL Consistency Check Pipeline — unified runner with full / flagged modes"
    )
    parser.add_argument("--input", required=True,
                        help="Path to input file (.pkl, .xlsx, .csv)")
    parser.add_argument("--mode", choices=["full", "flagged"], default="flagged",
                        help="Check mode: 'full' = all rows, 'flagged' = URL_CONSISTENCY_CHECK==1 only (default)")
    parser.add_argument("--all-rows", action="store_true",
                        help="[Legacy] Shortcut for --mode full")
    parser.add_argument("--output", default=None,
                        help="Output Excel path (default: output/results.xlsx)")
    parser.add_argument("--child-table", default=str(CHILD_TABLE_PATH),
                        help="Path to issuer_child_flagged.xlsx")
    parser.add_argument("--company-url-col", default=None,
                        help="Column containing the company's official URL for domain matching")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Skip LLM verification, use cache + rules only")
    parser.add_argument("--provider", choices=["anthropic", "vertex", "openai", "azure", "cowork"], default=None,
                        help="LLM provider: anthropic|vertex|openai|azure (API call) or 'cowork' (Claude in-session, exports unresolved)")
    parser.add_argument("--model", default=None,
                        help="LLM model / Azure deployment override")
    parser.add_argument("--eval", action="store_true",
                        help="Run evaluation against ground truth (needs COMMENT_CODE column)")
    parser.add_argument("--verdict-mode", choices=["v1", "v2", "v3", "v4", "v5"], default="v1",
                        help="Verdict output mode: "
                             "v1 = URLC01 & Manual Review only (default), "
                             "v2 = URLC01 + URLC02 + URLC03 + Manual Review, "
                             "v3 = URLC01 & Manual Review for FLAG==1 rows, "
                             "v4 = URLC01 (direct company) + URLC02 (ALL third-party incl exchanges) + Manual_Review (subsidiaries) + Tag column, "
                             "v5 = like v4 but allowlisted regulatory/exchange/gov portals → URLC03 + Tag column")
    args = parser.parse_args()

    # Legacy --all-rows flag → mode full
    mode = "full" if args.all_rows else args.mode

    # ── Resolve paths ────────────────────────────────────────────────────────
    input_path = resolve_path(args.input, PROJECT_ROOT)

    if args.output:
        output_path = resolve_path(args.output, PROJECT_ROOT)
    else:
        # Auto-name output based on input filename + verdict mode
        stem = input_path.stem
        vm = args.verdict_mode
        output_path = OUTPUT_DIR / f"{stem}_{vm}_results.xlsx"

    child_path = resolve_path(args.child_table, PROJECT_ROOT)

    # ── Load data ────────────────────────────────────────────────────────────
    log.info(f"Loading input data from {input_path}")
    full_df = load_input(str(input_path))
    log.info(f"Loaded {len(full_df)} total rows")

    # ── Determine which rows to classify ─────────────────────────────────────
    skip_mask = rows_to_skip_mask(full_df)
    has_skip_col = (
        "URL_CONSISTENCY_CHECK" in full_df.columns
        or (
            "URL_CONSISTENCY_FLAG" in full_df.columns
            and pd.api.types.is_numeric_dtype(full_df["URL_CONSISTENCY_FLAG"])
        )
    )

    if has_skip_col:
        fail_df = full_df[~skip_mask].copy()
        pass_df = full_df[skip_mask].copy() if skip_mask.any() else None
        log.info(
            f"MODE={mode}: {len(fail_df)} rows to validate, "
            f"{len(pass_df) if pass_df is not None else 0} skipped "
            f"('{SKIP_VERDICT}', CHECK/FLAG==0)"
        )
    elif mode == "flagged":
        log.warning(
            "No URL_CONSISTENCY_CHECK column — treating all rows as flagged.\n"
            "Tip: use --mode full explicitly if you want to classify everything."
        )
        fail_df = full_df.copy()
        pass_df = None
    else:
        fail_df = full_df.copy()
        pass_df = None
        log.info(f"MODE=full: Running pipeline on all {len(fail_df)} rows (no CHECK column)")

    if fail_df.empty and (pass_df is None or pass_df.empty):
        log.info("No rows to classify. Nothing to do.")
        return

    # ── Handle cowork provider (delegate to run_cowork_check logic) ────────
    if args.provider == "cowork":
        from run_cowork_check import step1_deterministic, step3_final
        import types
        # Build a minimal args-like object for cowork functions
        cowork_args = types.SimpleNamespace(
            child_table=args.child_table,
            company_url_col=args.company_url_col,
            eval=args.eval,
        )
        n = step1_deterministic(cowork_args, fail_df, pass_df)
        if n > 0:
            log.info(f"\n  {n} combos exported to output/cowork_unresolved.json")
            log.info(f"  → Claude in Cowork should classify these, then re-run with --step 3")
            log.info(f"  → Or re-run this command without --provider to use cache as-is\n")
        step3_final(cowork_args, fail_df, pass_df, output_path)
        log.info(f"Pipeline complete (mode={mode}, provider=cowork).")
        return

    # ── Run classification ───────────────────────────────────────────────────
    summary = run_classification(
        fail_df=fail_df,
        pass_df=pass_df,
        child_path=str(child_path),
        company_url_col=args.company_url_col,
        skip_llm=args.skip_llm,
        provider=args.provider,
        model=args.model,
        output_path=str(output_path),
        run_eval=args.eval,
        verdict_mode=args.verdict_mode,
    )

    log.info(f"Pipeline complete (mode={mode}, verdict_mode={args.verdict_mode}).")


if __name__ == "__main__":
    main()
