"""
Memory view — reads Hermes's structured memory stores and returns them
categorized + with capacity stats for the dashboard's Memory tab.

Two stores (real on-disk format is markdown with entries separated by `§`;
we also accept the JSON-array form `{"content": ...}` if present):
  - agent memory  : ~/.hermes/memories/{MEMORY.md | memory.json}
  - user profile  : ~/.hermes/memories/{USER.md  | user.json}

Pure read: no new backend state, no writes.
"""
import json
from pathlib import Path

MEM_DIR = Path.home() / ".hermes" / "memories"

# Soft capacity limit for the agent memory store (chars). Hermes keeps this
# store tight; past this it should be consolidated.
AGENT_CAP = 2200

# Category detection order matters: specific prefixes win over "mentions X".
CATEGORY_META = {
    "architecture": {"label": "Architecture", "color": "#a78bfa"},  # violet
    "projects":     {"label": "Projects",     "color": "#38bdf8"},  # sky
    "tools":        {"label": "Tools",        "color": "#2dd4bf"},  # teal
    "dashboard":    {"label": "Dashboard",    "color": "#60a5fa"},  # blue
    "llm":          {"label": "Local LLM",    "color": "#f472b6"},  # pink
    "user":         {"label": "User",         "color": "#fbbf24"},  # amber
    "general":      {"label": "General",      "color": "#a1a1aa"},  # zinc
}


def _categorize(content: str, is_user: bool) -> str:
    if is_user:
        return "user"
    c = content.strip()
    lower = c.lower()
    # Specific prefixes first.
    if c.startswith("[ARCHITECTURE]") or c.startswith("ARCHITECTURE:"):
        return "architecture"
    if c.startswith("[PROJECTS]") or c.startswith("Project memory"):
        return "projects"
    if c.startswith("Always loaded"):
        return "tools"
    if c.startswith("Dashboard:") or "dashboard" in lower:
        return "dashboard"
    if c.startswith("Local LLM") or "llm" in lower:
        return "llm"
    return "general"


def _read_store(md_name: str, json_name: str):
    """Return a list of entry-content strings from whichever store form exists.
    Prefers the JSON-array form (the documented format) then falls back to the
    real markdown form (entries separated by `§`)."""
    jpath = MEM_DIR / json_name
    if jpath.exists():
        try:
            data = json.loads(jpath.read_text())
            out = []
            for e in data:
                if isinstance(e, dict):
                    txt = str(e.get("content", "")).strip()
                elif isinstance(e, str):
                    txt = e.strip()
                else:
                    txt = str(e).strip()
                if txt:
                    out.append(txt)
            return out
        except Exception:
            pass  # fall through to markdown
    mpath = MEM_DIR / md_name
    if mpath.exists():
        try:
            raw = mpath.read_text()
            return [seg.strip() for seg in raw.split("§") if seg.strip()]
        except Exception:
            return []
    return []


def _build_entries(contents, is_user):
    entries = []
    for i, content in enumerate(contents):
        cat = _categorize(content, is_user)
        entries.append({
            "content": content,
            "category": cat,
            "chars": len(content),
            "source": "user" if is_user else "agent",
            # Stable position within its store — the edit/delete address (there
            # are no per-entry ids; index into the §-delimited store).
            "idx": i,
        })
    return entries


def _metabolism():
    """The consolidation worker's vital signs (~15-min cadence): capacity,
    provenance coverage, evict/merge/trim/archive counters. Phase 5 reframes
    the worker from invisible cron to visible organ — this is its EKG."""
    try:
        data = json.loads((MEM_DIR / "metabolism.json").read_text())
    except Exception:
        return None
    archive_dir = MEM_DIR / "archive"
    data["archive_entries"] = 0
    if archive_dir.exists():
        for f in archive_dir.glob("*.md"):
            try:
                data["archive_entries"] += f.read_text().count("\n## ")
            except Exception:
                pass
    return data


def build():
    agent_entries = _build_entries(_read_store("MEMORY.md", "memory.json"), is_user=False)
    user_entries = _build_entries(_read_store("USER.md", "user.json"), is_user=True)
    all_entries = agent_entries + user_entries

    agent_chars = sum(e["chars"] for e in agent_entries)
    total_chars = sum(e["chars"] for e in all_entries)

    per_category = {}
    for e in all_entries:
        c = e["category"]
        slot = per_category.setdefault(c, {"count": 0, "chars": 0})
        slot["count"] += 1
        slot["chars"] += e["chars"]

    return {
        "agent": agent_entries,
        "user": user_entries,
        "metabolism": _metabolism(),
        "categories": CATEGORY_META,
        "stats": {
            "total_entries": len(all_entries),
            "agent_entries": len(agent_entries),
            "user_entries": len(user_entries),
            "total_chars": total_chars,
            "agent_chars": agent_chars,
            "user_chars": sum(e["chars"] for e in user_entries),
            "capacity_limit": AGENT_CAP,
            "capacity_pct": round(agent_chars / AGENT_CAP * 100, 1) if AGENT_CAP else 0,
            "per_category": per_category,
        },
    }


# ---------------------------------------------------------------- mutation
# Edit/delete from the Memory tab. Addressed by (source, index) — index is the
# entry's position within its store, matching build()'s order. Every write
# timestamps a `.bak` first (the same convention the consolidation worker uses),
# so a bad edit is always recoverable.
import time
import shutil


def _store_files(source):
    return ("USER.md", "user.json") if source == "user" else ("MEMORY.md", "memory.json")


def _backup(path):
    try:
        shutil.copy2(path, path.with_name(f"{path.name}.bak.{int(time.time())}"))
    except Exception:
        pass


def _mutate_json(jpath, index, new_content):
    """Replace (str) or delete (None) the index-th non-empty entry in the JSON
    store, preserving each entry's shape (dict {content:…} vs bare string)."""
    try:
        data = json.loads(jpath.read_text())
    except Exception:
        return False, "store unreadable"
    if not isinstance(data, list):
        return False, "unexpected store shape"
    # Ordinal over the same non-empty entries build() surfaced.
    positions = []
    for i, e in enumerate(data):
        txt = (e.get("content", "") if isinstance(e, dict) else str(e)).strip()
        if txt:
            positions.append(i)
    if index < 0 or index >= len(positions):
        return False, "entry index out of range"
    di = positions[index]
    if new_content is None:
        del data[di]
    elif isinstance(data[di], dict):
        data[di]["content"] = new_content.strip()
    else:
        data[di] = new_content.strip()
    _backup(jpath)
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return True, None


def _mutate_markdown(mpath, index, new_content):
    """Replace (str) or delete (None) the index-th non-empty §-delimited entry,
    preserving the surrounding whitespace/format the store uses."""
    raw = mpath.read_text()
    parts = raw.split("§")
    nonempty = [i for i, seg in enumerate(parts) if seg.strip()]
    if index < 0 or index >= len(nonempty):
        return False, "entry index out of range"
    pi = nonempty[index]
    _backup(mpath)
    if new_content is None:
        del parts[pi]            # drops this segment + one adjacent § separator
    else:
        seg = parts[pi]
        lead = seg[:len(seg) - len(seg.lstrip())]
        trail = seg[len(seg.rstrip()):]
        parts[pi] = f"{lead}{new_content.strip()}{trail}"
    mpath.write_text("§".join(parts))
    return True, None


def _mutate(source, index, new_content):
    md_name, json_name = _store_files(source)
    jpath = MEM_DIR / json_name
    if jpath.exists():                       # JSON form wins (what _read_store prefers)
        ok, err = _mutate_json(jpath, index, new_content)
    else:
        mpath = MEM_DIR / md_name
        if not mpath.exists():
            return {"status": "error", "error": "memory store not found"}
        ok, err = _mutate_markdown(mpath, index, new_content)
    if not ok:
        return {"status": "error", "error": err}
    return {"status": "ok"}


def update_entry(source, index, content):
    if source not in ("agent", "user"):
        return {"status": "error", "error": "source must be 'agent' or 'user'"}
    if not (content or "").strip():
        return {"status": "error", "error": "content cannot be empty"}
    return _mutate(source, int(index), content)


def delete_entry(source, index):
    if source not in ("agent", "user"):
        return {"status": "error", "error": "source must be 'agent' or 'user'"}
    return _mutate(source, int(index), None)
