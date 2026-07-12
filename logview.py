#!/usr/bin/env python3
"""
logview.py — pretty, colored view of the copybot log.

The bot saves its log as raw JSON (so analyze.py / status.py can read it). This
tool renders that same file as clean, colored, human-readable lines.

    python3 logview.py               # show the last 40 events, prettied
    python3 logview.py --lines 100   # show the last 100
    python3 logview.py --follow      # live view — updates as new events arrive
    python3 logview.py --no-color    # plain text (for copying / piping)

Tip: use --follow instead of `tail -f copybot_log.jsonl` when you want the
nice view. Ctrl+C to stop watching (does NOT stop the bot).
"""

import argparse
import json
import os
import sys
import time

# reuse the exact formatter the bot uses, so live and replayed views match
from copybot import format_event, LOG_PATH


def emit(line, use_color):
    line = line.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
        print(format_event(ev, use_color=use_color), flush=True)
    except json.JSONDecodeError:
        print(line, flush=True)   # not JSON? show it raw rather than hide it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG_PATH)
    ap.add_argument("--lines", type=int, default=40, help="how many recent events to show")
    ap.add_argument("--follow", action="store_true", help="keep watching for new events")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    use_color = (not args.no_color) and sys.stdout.isatty()

    if not os.path.exists(args.log):
        print(f"No log file at {args.log}. Is the bot running in this folder?")
        return

    # show the tail first
    with open(args.log) as f:
        recent = f.readlines()
    for ln in recent[-args.lines:]:
        emit(ln, use_color)

    if not args.follow:
        return

    # then live-follow, tail -f style
    print("\n… following (Ctrl+C to stop watching) …\n")
    try:
        with open(args.log) as f:
            f.seek(0, os.SEEK_END)          # jump to the end; only show new lines
            while True:
                pos = f.tell()
                line = f.readline()
                if line:
                    emit(line, use_color)
                else:
                    time.sleep(0.5)
                    f.seek(pos)             # nothing new; wait and retry
    except KeyboardInterrupt:
        print("\nstopped watching.")


if __name__ == "__main__":
    main()
