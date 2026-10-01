#!/usr/bin/env python3
"""Results for the tutorial's Windows legs, from their execution records.

logos-windows-ci runs each leg as a reusable workflow, so the calling job has no
step of its own whose outcome says pass or fail. What every leg does leave is
its records: `doctest-execs-<caller>-<spec>/doctest-execs.json`, one entry per
step with a `status`. A leg passed only if it left records and every one passed;
no records at all means the cross-build failed or the leg never ran.

Usage:
    python3 scripts/windows-results.py execs/ --specs "tutorial-a tutorial-b" \
        --caller logos-release-set --pages-base URL -o results-windows-x86_64.json
"""

import argparse
import json
import os
import sys

PLATFORM = "windows-x86_64"


def result_for(spec, execs_dir, caller, pages_base):
    entry = {"spec": spec, "platform": PLATFORM, "runner": "windows-latest",
             "reportUrl": f"{pages_base.rstrip('/')}/{PLATFORM}/{spec}/"}
    path = os.path.join(execs_dir, f"doctest-execs-{caller}-{spec}", "doctest-execs.json")
    try:
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError) as exc:
        entry.update(status="failed",
                     reason=f"no execution records ({exc.__class__.__name__}): the "
                            "cross-build failed or the Windows leg did not run")
        return entry
    statuses = [run.get("status") for runs in records.values() for run in runs]
    failed = sum(1 for s in statuses if s != "pass")
    entry["status"] = "passed" if statuses and not failed else "failed"
    if entry["status"] == "failed":
        entry["reason"] = f"{failed} of {len(statuses)} step(s) failed" if statuses \
            else "the leg recorded no steps"
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("execs_dir")
    parser.add_argument("--specs", required=True, help="space-separated specs that ran")
    parser.add_argument("--caller", required=True, help="the calling repo's name")
    parser.add_argument("--pages-base", required=True)
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    results = [result_for(spec, args.execs_dir, args.caller, args.pages_base)
               for spec in args.specs.split()]
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
