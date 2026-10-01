#!/usr/bin/env python3
"""Write results-<platform>-<spec>.json for one doc-test job.

Shared by the doctests and tutorial jobs. Reads the job's state from the
environment: SPEC, PLATFORM, RUNNER_LABEL, OUTCOME (the run step's outcome),
SKIPPED (select-specs skip records), DESELECTED, and PAGES_BASE (where the
publish job puts the reports).
"""

import json
import os

spec     = os.environ["SPEC"]
platform = os.environ["PLATFORM"]
runner   = os.environ["RUNNER_LABEL"]
# One report per (spec, platform), so every cell of the validation
# table links to the report for exactly that cell.
base = f'{os.environ["PAGES_BASE"].rstrip("/")}/{platform}/{spec}/'

skipped = json.loads(os.environ.get("SKIPPED") or "[]")
if skipped:
    # select-specs already explained why, per platform.
    results = skipped
elif os.environ.get("DESELECTED") == "true":
    results = [{"spec": spec, "platform": platform, "status": "skipped",
                "reason": "not in the requested spec list"}]
else:
    status = {"success": "passed", "failure": "failed",
              "skipped": "skipped", "": "skipped"}.get(os.environ["OUTCOME"], "failed")
    results = [{"spec": spec, "platform": platform,
                "status": status, "reportUrl": base}]

for entry in results:
    entry.setdefault("runner", runner)

with open(f"results-{platform}-{spec}.json", "w") as handle:
    json.dump(results, handle, indent=2)
print(json.dumps(results, indent=2))
