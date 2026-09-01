#!/usr/bin/env bash
# One-way, bounded sync from orchestratormaxxing governed project memory to Hermes MEMORY.md.
# USER.md is deliberately outside this tool's scope.
set -euo pipefail

MEMORY_BRIDGE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MEMORY_BRIDGE_SCRIPT_DIR

exec python3 - "$@" <<'PY'
import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path


DELIMITER = "\n§\n"
MANAGED_RE = re.compile(r"^\[orchestratormaxxing\] \[([a-z0-9][a-z0-9-]{0,79})\] ")
INDEX_RE = re.compile(r"^- \[([^]]+)\]\(([^)]+\.md)\) — (.+)$")
TTL_DAYS = {"project": 14, "reference": 30}
INCLUDE_RE = re.compile(
    r"\b(?:provider|routing|runtime|service|config(?:uration)?|tool(?:ing)?|fleet|"
    r"dashboard|platform|plugin|workflow|memory|browser|chrome|open-design|"
    r"self-improve|scheduler|integration|worker|gpu-desktop)\b|(?:^|-)current(?:-|$)",
    re.IGNORECASE,
)
CODE_SPECIFIC_RE = re.compile(
    r"\b(?:unit test|test fixture|function|class|endpoint|refactor|css|javascript|"
    r"python module|source line|implementation detail|code-specific|sidecar|"
    r"sql write|kanban verb|synthetic status)\b",
    re.IGNORECASE,
)
PRIORITY_RE = re.compile(
    r"\b(?:hermes|provider|routing|runtime|service|config|dashboard|platform|"
    r"plugin|fleet|browser|chrome|open-design|self-improve|memory)\b",
    re.IGNORECASE,
)


class BridgeError(RuntimeError):
    pass


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}
    fields = {}
    for line in lines[1:end]:
        match = re.match(r"^\s*([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            fields[match.group(1)] = unquote(match.group(2))
    return fields


def parse_day(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def default_source():
    override = os.environ.get("ORCHESTRATORMAXXING_MEMORY_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    script_dir = Path(os.environ["MEMORY_BRIDGE_SCRIPT_DIR"])
    candidates = (
        script_dir.parent / ".agents" / "memory",
        Path.home() / "dev" / "orchestratormaxxing" / ".agents" / "memory",
        Path.home() / "Dev" / "orchestratormaxxing" / ".agents" / "memory",
    )
    return next((path for path in candidates if path.is_dir()), candidates[1])


def relevant(name, description, fields, today):
    if (fields.get("status") or "active").lower() != "active":
        return False, "inactive"
    if (fields.get("sensitivity") or "normal").lower() != "normal":
        return False, "sensitive"
    memory_type = (fields.get("type") or "project").lower()
    if memory_type not in TTL_DAYS:
        return False, "non-project"
    valid_to = parse_day(fields.get("valid_to"))
    if valid_to and today > valid_to:
        return False, "stale"
    verified = parse_day(fields.get("last_verified") or fields.get("created"))
    if not verified or (today - verified).days > TTL_DAYS[memory_type]:
        return False, "stale"
    explicit = (fields.get("hermes_sync") or "").lower()
    if explicit in ("false", "no", "skip"):
        return False, "policy"
    haystack = f"{name} {description}"
    if explicit not in ("true", "yes", "sync") and not INCLUDE_RE.search(haystack):
        return False, "irrelevant"
    if explicit not in ("true", "yes", "sync") and CODE_SPECIFIC_RE.search(haystack):
        return False, "code-specific"
    return True, "selected"


def load_candidates(source, today):
    index = source / "MEMORY.md"
    if not index.is_file():
        raise BridgeError(f"shared memory index not found: {index}")
    selected = []
    skipped = {}
    for line in index.read_text(encoding="utf-8").splitlines():
        match = INDEX_RE.match(line)
        if not match:
            continue
        name, filename, description = match.groups()
        path = source / filename
        if not path.is_file() or path.resolve().parent != source.resolve():
            skipped[name] = "missing-linked-file"
            continue
        fields = frontmatter(path.read_text(encoding="utf-8"))
        keep, reason = relevant(name, description, fields, today)
        if not keep:
            skipped[name] = reason
            continue
        verified = parse_day(fields.get("last_verified") or fields.get("created"))
        priority = 0 if PRIORITY_RE.search(f"{name} {description}") else 1
        selected.append({
            "name": name,
            "entry": f"[orchestratormaxxing] [{name}] {description}",
            "priority": priority,
            "verified": verified or date.min,
        })
    selected.sort(key=lambda row: (row["priority"], -row["verified"].toordinal(), row["name"]))
    return selected, skipped


def entries(raw):
    if not raw.strip():
        return []
    return [entry.strip() for entry in raw.split(DELIMITER) if entry.strip()]


@contextlib.contextmanager
def hermes_lock(memory_file):
    lock_path = memory_file.with_suffix(memory_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write(path, text):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_log(path, message):
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def sync(args):
    today = date.fromisoformat(args.today) if args.today else date.today()
    source = args.source_dir.expanduser().resolve()
    memory_file = args.memory_file.expanduser().resolve()
    log_file = args.log_file.expanduser().resolve() if args.log_file else memory_file.parent.parent / "logs" / "memory-bridge.log"
    candidates, skipped = load_candidates(source, today)

    with hermes_lock(memory_file):
        if memory_file.exists():
            try:
                raw = memory_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise BridgeError(f"refusing to overwrite unreadable Hermes memory: {exc}") from exc
        else:
            raw = ""
        old_entries = entries(raw)
        native = []
        old_managed = {}
        for entry in old_entries:
            match = MANAGED_RE.match(entry)
            if match:
                old_managed[match.group(1)] = entry
            else:
                native.append(entry)

        if len(DELIMITER.join(native)) > args.max_chars:
            raise BridgeError("native Hermes memory already exceeds the configured character limit")

        chosen = []
        omitted_cap = []
        for row in candidates:
            proposal = native + chosen + [row["entry"]]
            if len(DELIMITER.join(proposal)) <= args.max_chars:
                chosen.append(row["entry"])
            else:
                omitted_cap.append(row["name"])
        new_entries = native + chosen
        new_raw = DELIMITER.join(new_entries)
        if new_raw:
            new_raw += "\n"

        new_managed = {MANAGED_RE.match(entry).group(1): entry for entry in chosen}
        added = sorted(set(new_managed) - set(old_managed))
        removed = sorted(set(old_managed) - set(new_managed))
        updated = sorted(name for name in set(new_managed) & set(old_managed)
                         if new_managed[name] != old_managed[name])
        changed = raw != new_raw
        summary = (
            f"synced added={len(added)} updated={len(updated)} removed={len(removed)} "
            f"active={len(new_managed)} chars={len(new_raw.rstrip())}/{args.max_chars} "
            f"skipped={len(skipped)} omitted_at_capacity={len(omitted_cap)}"
        )
        if args.dry_run:
            print("dry-run " + summary)
            if added:
                print("would_add: " + ", ".join(added))
            if updated:
                print("would_update: " + ", ".join(updated))
            if removed:
                print("would_remove: " + ", ".join(removed))
            if omitted_cap:
                print("omitted_at_capacity: " + ", ".join(omitted_cap))
            return
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        if changed:
            if memory_file.exists():
                backup = memory_file.with_name(f"{memory_file.name}.bak.{int(time.time())}")
                shutil.copy2(memory_file, backup)
            atomic_write(memory_file, new_raw)
        append_log(log_file, summary)
        print(summary)
        if added:
            print("added: " + ", ".join(added))
        if updated:
            print("updated: " + ", ".join(updated))
        if removed:
            print("removed: " + ", ".join(removed))
        if omitted_cap:
            print("omitted_at_capacity: " + ", ".join(omitted_cap))


def main():
    parser = argparse.ArgumentParser(description="sync governed orchestratormaxxing facts into Hermes MEMORY.md")
    parser.add_argument("--source-dir", type=Path, default=default_source())
    parser.add_argument("--memory-file", type=Path,
                        default=Path(os.environ.get("HERMES_MEMORY_FILE", Path.home() / ".hermes" / "memories" / "MEMORY.md")))
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--max-chars", type=int, default=2200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--today", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.max_chars < 1:
        parser.error("--max-chars must be positive")
    try:
        sync(args)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"memory-bridge-hermes: {exc}", file=sys.stderr)
        return 1
    return 0


raise SystemExit(main())
PY
