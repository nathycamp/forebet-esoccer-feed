"""python -m prediction_engine < already-downloaded-feed.json"""

import argparse
import json
import sys
from datetime import datetime, timezone

from . import predict_feed
from .validation import utc


def main():
    parser = argparse.ArgumentParser(description="Stateless Forebet prediction engine; no network or database")
    parser.add_argument("input", nargs="?", default="-", help="JSON file, or - for stdin")
    parser.add_argument("--as-of", help="UTC ISO timestamp for reproducible analysis; omit in production")
    analysis = parser.add_mutually_exclusive_group()
    analysis.add_argument("--backtest", action="store_true", help="Retrospective form-only diagnostic on supplied results")
    analysis.add_argument("--compare", action="store_true", help="Compare three fixed score models on the supplied results")
    analysis.add_argument("--compare-opponents", action="store_true", help="Compare with/without the common-opponent adjustment")
    parser.add_argument("--limit", type=int, default=30, help="Maximum backtest fixtures per league (default 30)")
    args = parser.parse_args()
    try:
        if args.input == "-":
            feed = json.load(sys.stdin)
        else:
            with open(args.input) as source:
                feed = json.load(source)
        now = utc(args.as_of) if args.as_of else datetime.now(timezone.utc)
        if args.compare or args.compare_opponents:
            from .evaluation import compare_models
            result = compare_models(feed, limit=args.limit, common_opponents=args.compare_opponents)
        elif args.backtest:
            from .evaluation import backtest_feed
            result = backtest_feed(feed, limit=args.limit)
        else:
            result = predict_feed(feed, now=now)
        json.dump(result, sys.stdout, allow_nan=False, indent=2)
        sys.stdout.write("\n")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Input error: {exc}\n")


if __name__ == "__main__":
    main()
