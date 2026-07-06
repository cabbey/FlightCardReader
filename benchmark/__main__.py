#!/usr/bin/env python3
"""Benchmark suite entry point.

Provides a unified CLI to run individual steps or the full pipeline:

    python -m benchmark export   -- export dataset from DB
    python -m benchmark run      -- run models against dataset
    python -m benchmark score    -- score results and generate report
    python -m benchmark full     -- run the full pipeline (export + run + score)

Each subcommand accepts the same arguments as its standalone module.
Run `python -m benchmark <subcommand> --help` for details.
"""

import sys


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Subcommands:")
        print("  export    Export human-verified records to a benchmark dataset")
        print("  run       Run extraction benchmark with specified models")
        print("  score     Score results and generate comparison report")
        print("  full      Run all steps sequentially")
        print()
        print("Usage: python -m benchmark <subcommand> [args...]")
        sys.exit(0)

    subcommand = sys.argv[1]
    # Remove the subcommand from argv so argparse in each module works correctly
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if subcommand == "export":
        from benchmark.export_dataset import main as export_main
        export_main()
    elif subcommand == "run":
        from benchmark.runner import main as runner_main
        runner_main()
    elif subcommand == "score":
        from benchmark.report import main as report_main
        report_main()
    elif subcommand == "full":
        print("Full pipeline not yet supported via single command.")
        print("Run each step separately:")
        print("  python -m benchmark export --db <path> --image-dir <path>")
        print("  python -m benchmark run --models <model1> <model2> ...")
        print("  python -m benchmark score")
        sys.exit(1)
    else:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        print("Run 'python -m benchmark --help' for usage.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
