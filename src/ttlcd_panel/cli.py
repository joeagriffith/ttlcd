"""`panel` — a small CLI over the daemon API for quick interactions."""
import argparse
import json
import os
import sys

import requests

DEFAULT_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8770")


def _post(url, path, payload):
    return requests.post(url + path, json=payload, timeout=2.0)


def _get(url, path):
    return requests.get(url + path, timeout=2.0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="panel", description="Talk to the LCD panel daemon")
    p.add_argument("--url", default=DEFAULT_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("msg", help="flash a message on the panel")
    m.add_argument("text")
    m.add_argument("--level", default="info", choices=["info", "warn", "error"])
    m.add_argument("--duration", type=float, default=5.0)

    sub.add_parser("status", help="show daemon + panel health")
    sub.add_parser("system", help="dump the latest system/GPU snapshot")
    sub.add_parser("run", help="show the current run state")

    v = sub.add_parser("view", help="set the idle view")
    v.add_argument("name", choices=["system", "mascot"])

    i = sub.add_parser("issue", help="file an issue to the lead")
    i.add_argument("--title", required=True)
    i.add_argument("--body", default="")
    i.add_argument("--agent", default="cli")
    i.add_argument("--severity", default="medium", choices=["low", "medium", "high"])

    args = p.parse_args(argv)
    try:
        if args.cmd == "msg":
            _post(args.url, "/message", {"text": args.text, "level": args.level, "duration": args.duration})
            print("sent.")
        elif args.cmd == "status":
            r = _get(args.url, "/health").json()
            print(json.dumps(r, indent=2))
        elif args.cmd == "system":
            print(json.dumps(_get(args.url, "/system").json(), indent=2))
        elif args.cmd == "run":
            print(json.dumps(_get(args.url, "/run").json(), indent=2))
        elif args.cmd == "view":
            r = _post(args.url, "/view", {"name": args.name}).json()
            print("ok" if r.get("ok") else "unknown view")
        elif args.cmd == "issue":
            _post(args.url, "/issue", {"title": args.title, "body": args.body,
                                       "agent": args.agent, "severity": args.severity})
            print("filed.")
    except requests.exceptions.RequestException as e:
        print(f"panel: daemon unreachable at {args.url} ({e.__class__.__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
