"""Post a summary of the cost-sync run to Slack via Incoming Webhook.

Runs as the final workflow step with `if: always()` so the channel hears about
both successes and failures. Tolerates a missing webhook so the cost sync
itself never breaks if Slack is misconfigured or down.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
RUN_URL = os.environ.get("RUN_URL", "")
JOB_STATUS = (sys.argv[1] if len(sys.argv) > 1 else "unknown").lower()


def main() -> int:
    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL not set; skipping Slack notification.")
        return 0

    reports = sorted(glob.glob("cost_sync_report_*.csv"))
    report = reports[-1] if reports else None
    date = _date_from_report(report) or dt.date.today().isoformat()

    if not report:
        msg = _failure_message(date, "no report CSV produced")
    else:
        msg = _summary_message(date, report)

    payload = json.dumps(msg).encode()
    req = urllib.request.Request(
        WEBHOOK, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print("Slack notification sent.")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        # Don't fail the job over a Slack hiccup.
        print(f"Slack notification failed (non-fatal): {e}")
    return 0


def _date_from_report(path: str | None) -> str | None:
    if not path:
        return None
    m = re.search(r"cost_sync_report_(\d{4}-\d{2}-\d{2})", path)
    return m.group(1) if m else None


def _failure_message(date: str, reason: str) -> dict:
    text = (
        f":x: *Cost sync — {date}*\n"
        f"Workflow finished with status `{JOB_STATUS}` — {reason}.\n"
        + (f"<{RUN_URL}|View run>" if RUN_URL else "")
    )
    return {"text": text}


def _summary_message(date: str, report_path: str) -> dict:
    with open(report_path, newline="") as f:
        rows = list(csv.DictReader(f))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    updated = [r for r in rows if r["status"] == "updated"]
    errored = [r for r in rows if r["status"] == "error"]
    swings = counts.get("skipped_swing", 0)
    unchanged = counts.get("unchanged", 0)

    if errored:
        icon = ":warning:"
    elif JOB_STATUS != "success":
        icon = ":x:"
    else:
        icon = ":white_check_mark:"

    summary_line = (
        f"{icon} *{len(updated)}* updated · *{len(errored)}* errors · "
        f"*{swings}* skipped (>50% swing) · *{unchanged}* unchanged"
    )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Weekly cost sync — {date}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_line}},
    ]

    if updated:
        lines = [
            f"`{r['sku']}`  ${r['current_cost'] or '—'} → ${r['new_cost']}"
            for r in updated[:25]
        ]
        if len(updated) > 25:
            lines.append(f"_…and {len(updated) - 25} more (see report artifact)_")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Updated:*\n" + "\n".join(lines)},
            }
        )

    if errored:
        lines = []
        for r in errored[:10]:
            note = (r.get("note") or "").replace("\n", " ")[:250]
            lines.append(f"`{r['sku']}` — {note}")
        if len(errored) > 10:
            lines.append(f"_…and {len(errored) - 10} more_")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Errors:*\n" + "\n".join(lines)},
            }
        )

    if RUN_URL:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{RUN_URL}|GitHub Actions run>"}],
            }
        )

    fallback = f"Cost sync {date} — {len(updated)} updated, {len(errored)} errors"
    return {"blocks": blocks, "text": fallback}


if __name__ == "__main__":
    sys.exit(main())
