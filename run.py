#!/usr/bin/env python3
"""ENAC-IT4R Dataset & Repository Harvester - application entry point.

Usage
-----
    python run.py                                   # start the dashboard
    python run.py --port 9000 --open                # custom port, open browser
    python run.py --harvest --days 7                # headless 7-day harvest
    python run.py --harvest --from 2026-01-01 --to 2026-03-31 --type software

EPFL ENAC-IT4R - IT for Research, School of Architecture, Civil and
Environmental Engineering.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from backend import config
from backend.logger import configure_logging

DATE_FORMAT = "%Y-%m-%d"


def build_parser() -> argparse.ArgumentParser:
    """Command line interface definition."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Harvest EPFL research outputs from Infoscience (DSpace 7) and the "
            "Zenodo EPFL community, then explore them in an EPFL-styled dashboard."
        ),
    )
    parser.add_argument("--host", default=config.HOST, help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=config.PORT, help="HTTP port (default: %(default)s)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument(
        "--harvest",
        action="store_true",
        help="run a headless harvest, print a summary and exit",
    )
    parser.add_argument("--from", dest="date_from", default=None, help="window start (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", default=None, help="window end (YYYY-MM-DD)")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="window length when --from is omitted (default: %(default)s)",
    )
    parser.add_argument(
        "--type",
        dest="resource_filter",
        default=config.DEFAULT_RESOURCE_FILTER,
        choices=list(config.RESOURCE_FILTERS.keys()),
        help="resource type filter (default: %(default)s)",
    )
    return parser


def run_headless_harvest(args: argparse.Namespace) -> int:
    """Execute one harvest without starting the web server."""
    from backend.harvester import HarvestBusyError, execute_harvest, resolve_window

    try:
        start, end = resolve_window(args.date_from, args.date_to, args.days)
    except ValueError as exc:
        print(f"Invalid date window: {exc}", file=sys.stderr)
        return 2

    print(
        f"Harvesting {config.RESOURCE_FILTERS[args.resource_filter]['label']} "
        f"from {start.isoformat()} to {end.isoformat()} ..."
    )
    try:
        payload = execute_harvest(
            start.isoformat(), end.isoformat(), args.resource_filter, args.days
        )
    except HarvestBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    counts = payload["counts"]
    sources = payload["sources"]
    print("")
    print(f"Window            : {payload['date_from']} -> {payload['date_to']}")
    print(f"Zenodo            : {sources['zenodo']['status']} ({sources['zenodo']['found']} records)")
    print(
        f"Infoscience       : {sources['infoscience']['status']} "
        f"({sources['infoscience']['found']} records)"
    )
    print(f"Merged by DOI     : {counts['merged']}")
    print(f"New / updated     : {counts['new']} / {counts['updated']}")
    print(f"Unchanged         : {counts['unchanged']}")
    print(f"Database total    : {counts['stored']}")
    print(f"JSON mirror       : {payload['mirror']['path']}")
    print(f"Duration          : {payload['duration_seconds']}s")
    return 0


def main(argv: list = None) -> int:
    """Program entry point."""
    args = build_parser().parse_args(argv)
    logger = configure_logging()

    try:
        import requests  # noqa: F401 - fail fast with a helpful message
    except ImportError:
        print(
            "Missing dependency 'requests'. Install the requirements first:\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    if args.harvest:
        return run_headless_harvest(args)

    from backend.server import serve

    if args.open:
        url = f"http://{args.host}:{args.port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        serve(args.host, args.port)
    except OSError as exc:
        logger.error("Cannot start the server on %s:%d - %s", args.host, args.port, exc)
        logger.error("Another process may already use that port; try --port 8081.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
