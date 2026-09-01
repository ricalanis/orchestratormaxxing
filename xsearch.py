#!/usr/bin/env python3
"""
xsearch — query xAI / Grok with the Agent Tools API (web + X search).

Usage:
    ./xsearch.py "your search query"
    ./xsearch.py "query" --days 14 --sources x,web --model grok-4.3
    ./xsearch.py "query" --from 2026-05-17 --to 2026-05-31
    ./xsearch.py "query" --json    # raw response

Server-side tools (xAI Agent Tools API): web_search, x_search, code_interpreter.
Reads XAI_API_KEY from the environment or a sibling .env file.
Docs: https://docs.x.ai/docs/guides/tools/overview
"""
import argparse
import datetime as dt
import json
import os
import ssl
import sys
import urllib.request
import urllib.error

API_URL = "https://api.x.ai/v1/responses"

# map friendly --sources names to xAI tool types
SOURCE_TO_TOOL = {
    "x": "x_search",
    "web": "web_search",
    "news": "web_search",  # no separate news tool; web_search covers news
}


def ssl_context():
    """Build an SSL context with a working CA bundle (macOS python.org fix)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _read_env_file(path):
    """Return XAI_API_KEY from a KEY=value file, or None."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "XAI_API_KEY":
                return v.strip()
    return None


def load_env():
    # 1) environment, 2) standalone global config, 3) sibling .env (repo dev)
    if os.environ.get("XAI_API_KEY"):
        return os.environ["XAI_API_KEY"]
    candidates = [
        os.path.expanduser("~/.config/claudemaxxing/.env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    for path in candidates:
        key = _read_env_file(path)
        if key:
            return key
    sys.exit("ERROR: XAI_API_KEY not found in env, ~/.config/claudemaxxing/.env, or sibling .env")


def main():
    p = argparse.ArgumentParser(description="xAI/Grok Agent Tools search")
    p.add_argument("query", help="search query / question")
    p.add_argument("--days", type=int, default=14, help="lookback window in days (default 14)")
    p.add_argument("--from", dest="from_date", help="YYYY-MM-DD (overrides --days)")
    p.add_argument("--to", dest="to_date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--sources", default="x,web", help="comma list: x,web,news")
    p.add_argument("--model", default="grok-4.3", help="model id (default grok-4.3)")
    p.add_argument("--json", action="store_true", help="print raw JSON response")
    args = p.parse_args()

    to_date = args.to_date or dt.date.today().isoformat()
    if args.from_date:
        from_date = args.from_date
    else:
        from_date = (dt.date.fromisoformat(to_date) - dt.timedelta(days=args.days)).isoformat()

    # dedupe tool types while preserving order
    tools, seen = [], set()
    for s in args.sources.split(","):
        t = SOURCE_TO_TOOL.get(s.strip())
        if t and t not in seen:
            seen.add(t)
            tools.append({"type": t, "from_date": from_date, "to_date": to_date})

    system = (
        "You are a research assistant. Search live sources and report concrete, recent "
        "posts, videos, threads, and discussions. For each item give: author/handle or "
        "outlet, platform, a one-line summary, and the date. Be specific, cite URLs, and "
        "do not invent results. If few results exist, say so plainly."
    )

    payload = {
        "model": args.model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": args.query},
        ],
        "tools": tools,
    }

    key = load_env()
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300, context=ssl_context()) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}")

    if args.json:
        print(json.dumps(data, indent=2))
        return

    # extract final assistant text + citations from the responses output array
    text_parts, citations = [], []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                text_parts.append(c.get("text", ""))
                for ann in c.get("annotations") or []:
                    url = ann.get("url")
                    if url and url not in citations:
                        citations.append(url)

    n_searches = sum(1 for i in data.get("output", []) if i.get("type", "").endswith("_search_call"))
    print(f"# Query: {args.query}")
    print(f"# Window: {from_date} → {to_date}  | sources: {args.sources}  | model: {args.model}")
    print(f"# Tool searches performed: {n_searches}\n")
    print("\n".join(text_parts).strip() or "(no text returned)")
    if citations:
        print("\n## Citations")
        for i, c in enumerate(citations, 1):
            print(f"{i}. {c}")


if __name__ == "__main__":
    main()
