"""
Knowledge-graph memory for the Hermes orchestrator.

A typed graph (nodes + edges) in its own SQLite file — the connective tissue
between the existing memory silos (kanban tasks, git history, research notes,
MEMORY.md decisions). Vector/text search finds "what's relevant"; this graph
answers "how is it connected, who decided what, what depends on what."

Design (per knowledge/research-knowledge-graph-memory.md):
  - Storage: SQLite, two tables (nodes, edges). No server, no Neo4j.
  - Reasoning: plain SQL + a small in-Python BFS for subgraph expansion
    (NetworkX would work but adds a dep for what a 30-line BFS does).
  - Entity extraction: deterministic vocabulary matching + Obsidian-style
    [[wikilinks]] — NOT spaCy. Keeping the tool/dep frontier minimal is the
    house doctrine; a curated concept vocab is reproducible and zero-dep.

Idempotent: node IDs are stable, deterministic keys (`type:key`), so
re-ingesting updates in place rather than duplicating. `rebuild()` wipes first
for a clean rebuild.
"""
import os
import sqlite3
import json
import re
import time
import hashlib
import subprocess
import sys
from pathlib import Path

# Phase 5: the ingest roots are PARAMETERS (env-overridable), not hardcoded to
# one repo — any project can point the graph at its own dirs.
GRAPH_DB = Path(os.environ.get("GRAPH_DB", str(Path.home() / ".hermes" / "graph_memory.db")))
REPO_DIR = Path(os.environ.get("GRAPH_REPO_DIR", str(Path.home() / "dev" / "orchestratormaxxing")))
KNOWLEDGE_DIR = Path(os.environ.get("GRAPH_KNOWLEDGE_DIR", str(REPO_DIR / "knowledge")))
MEMORY_MD = Path(os.environ.get("GRAPH_MEMORY_MD", str(Path.home() / ".hermes" / "memories" / "MEMORY.md")))
CC_MEMORY_ROOT = Path(os.environ.get("GRAPH_CC_MEMORY_ROOT", str(Path.home() / ".claude" / "projects")))
OBSIDIAN_DIR = Path(os.environ.get("GRAPH_OBSIDIAN_DIR", str(Path.home() / "Documents" / "Obsidian Vault")))

NODE_TYPES = ["Project", "Task", "Decision", "Note", "Session", "Concept", "Agent",
              "Skill", "Commit", "Sprint"]
EDGE_TYPES = ["MENTIONS", "PART_OF", "DECIDED_IN", "RESULTED_IN", "DERIVED_FROM",
              "IMPLEMENTS", "ASSIGNED_TO", "DEPENDS_ON", "AUTHORED_BY", "LINKS_TO"]

# Deterministic concept vocabulary — canonical label → regex alternation of
# surface forms, matched case-insensitively to draw MENTIONS edges. Phase 5:
# lives in a DATA file (concept_vocab.json) so extending the vocabulary is an
# edit, not a code change. Reproducible and dependency-free (no NER model).
_VOCAB_FILE = Path(os.environ.get(
    "GRAPH_CONCEPT_VOCAB", str(Path(__file__).parent / "concept_vocab.json")))

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import orchestration_practices as _practices  # noqa: E402


def _load_vocab() -> dict:
    vocab = {}
    try:
        vocab.update(json.loads(_VOCAB_FILE.read_text()))
    except Exception:
        pass
    return vocab


CONCEPT_VOCAB = _load_vocab()
_COMPILED_VOCAB = {label: re.compile(pat, re.IGNORECASE) for label, pat in CONCEPT_VOCAB.items()}


# ─────────────────────────── helpers ───────────────────────────

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:80] or "x"


def _hash(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _now() -> int:
    return int(time.time())


# ─────────────────────────── store ───────────────────────────

class GraphStore:
    def __init__(self, db_path: Path = GRAPH_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER,
                    updated_at INTEGER,
                    properties_json TEXT
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    src_id TEXT NOT NULL,
                    dst_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at INTEGER,
                    properties_json TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type)")

    # ---- writes ----

    def add_node(self, type: str, label: str, node_id: str = None,
                 properties: dict = None, created_at: int = None) -> str:
        """Upsert a node. If node_id omitted, a stable id `type:slug(label)` is
        derived so repeated ingests update in place instead of duplicating."""
        nid = node_id or f"{type.lower()}:{_slug(label)}"
        now = _now()
        props = json.dumps(properties or {}, ensure_ascii=False)
        with self._conn() as c:
            c.execute("""
                INSERT INTO nodes (id, type, label, created_at, updated_at, properties_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label,
                    type=excluded.type,
                    updated_at=excluded.updated_at,
                    properties_json=excluded.properties_json
            """, (nid, type, label, created_at or now, now, props))
        return nid

    def add_edge(self, src_id: str, dst_id: str, type: str,
                 properties: dict = None) -> str:
        """Upsert a typed edge. Edge id is a hash of (src, type, dst) so the same
        relationship is never stored twice."""
        eid = _hash(src_id, type, dst_id)
        props = json.dumps(properties or {}, ensure_ascii=False)
        with self._conn() as c:
            c.execute("""
                INSERT INTO edges (id, src_id, dst_id, type, created_at, properties_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    properties_json=excluded.properties_json
            """, (eid, src_id, dst_id, type, _now(), props))
        return eid

    def clear(self):
        with self._conn() as c:
            c.execute("DELETE FROM edges")
            c.execute("DELETE FROM nodes")

    # ---- reads ----

    def get_node(self, node_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return _node_dict(row) if row else None

    def search(self, query: str, type: str = None, limit: int = 50,
               include_archived: bool = False) -> list[dict]:
        """Nodes whose label matches ``query`` (case-insensitive substring).
        Archived nodes are excluded by default (HIGH fix from Kimi review)."""
        sql = "SELECT * FROM nodes WHERE label LIKE ? COLLATE NOCASE"
        params: list = [f"%{query}%"]
        if type:
            sql += " AND type=?"
            params.append(type)
        if not include_archived:
            sql += " AND (properties_json IS NULL OR json_extract(properties_json, '$.status') IS NULL OR json_extract(properties_json, '$.status') != 'archived')"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [_node_dict(r) for r in rows]

    def neighbors(self, node_id: str) -> list[dict]:
        """Adjacent nodes with the connecting edge. Both directions."""
        with self._conn() as c:
            rows = c.execute("""
                SELECT e.id AS eid, e.type AS etype, e.src_id, e.dst_id, e.properties_json AS eprops,
                       n.* FROM edges e
                JOIN nodes n ON n.id = CASE WHEN e.src_id=? THEN e.dst_id ELSE e.src_id END
                WHERE e.src_id=? OR e.dst_id=?
            """, (node_id, node_id, node_id)).fetchall()
        out = []
        for r in rows:
            nd = _node_dict(r)
            nd["edge"] = {
                "id": r["eid"], "type": r["etype"],
                "src": r["src_id"], "dst": r["dst_id"],
                "direction": "out" if r["src_id"] == node_id else "in",
            }
            out.append(nd)
        return out

    def expand(self, node_id: str, hops: int = 2, cap: int = 300) -> dict:
        """BFS `hops` out from node_id. Returns {nodes, edges} subgraph JSON."""
        if not self.get_node(node_id):
            return {"root": node_id, "nodes": [], "edges": [], "found": False}
        seen_nodes = {node_id}
        frontier = {node_id}
        edge_ids = {}
        with self._conn() as c:
            for _ in range(max(0, hops)):
                if not frontier or len(seen_nodes) >= cap:
                    break
                placeholders = ",".join("?" * len(frontier))
                rows = c.execute(f"""
                    SELECT * FROM edges
                    WHERE src_id IN ({placeholders}) OR dst_id IN ({placeholders})
                """, (*frontier, *frontier)).fetchall()
                nxt = set()
                for e in rows:
                    edge_ids[e["id"]] = _edge_dict(e)
                    for other in (e["src_id"], e["dst_id"]):
                        if other not in seen_nodes and len(seen_nodes) < cap:
                            seen_nodes.add(other)
                            nxt.add(other)
                frontier = nxt
            # Materialize node rows.
            node_rows = []
            ids = list(seen_nodes)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                node_rows += c.execute(f"SELECT * FROM nodes WHERE id IN ({ph})", chunk).fetchall()
        nodes = [_node_dict(r) for r in node_rows]
        present = {n["id"] for n in nodes}
        # Only keep edges whose endpoints are both in the returned node set.
        edges = [e for e in edge_ids.values() if e["src"] in present and e["dst"] in present]
        return {"root": node_id, "hops": hops, "found": True, "nodes": nodes, "edges": edges}

    def all_graph(self, cap: int = 600, include_archived: bool = False) -> dict:
        with self._conn() as c:
            sql = "SELECT * FROM nodes ORDER BY updated_at DESC LIMIT ?"
            params: list = [cap]
            if not include_archived:
                sql = "SELECT * FROM nodes WHERE (properties_json IS NULL OR json_extract(properties_json, '$.status') IS NULL OR json_extract(properties_json, '$.status') != 'archived') ORDER BY updated_at DESC LIMIT ?"
            nrows = c.execute(sql, params).fetchall()
            present = {r["id"] for r in nrows}
            erows = c.execute("SELECT * FROM edges").fetchall()
        nodes = [_node_dict(r) for r in nrows]
        edges = [_edge_dict(e) for e in erows if e["src_id"] in present and e["dst_id"] in present]
        return {"nodes": nodes, "edges": edges}

    def stats(self) -> dict:
        with self._conn() as c:
            n_by_type = {r["type"]: r["n"] for r in
                         c.execute("SELECT type, COUNT(*) n FROM nodes GROUP BY type").fetchall()}
            e_by_type = {r["type"]: r["n"] for r in
                         c.execute("SELECT type, COUNT(*) n FROM edges GROUP BY type").fetchall()}
            total_n = c.execute("SELECT COUNT(*) n FROM nodes").fetchone()["n"]
            total_e = c.execute("SELECT COUNT(*) n FROM edges").fetchone()["n"]
        return {
            "nodes": total_n,
            "edges": total_e,
            "concepts": n_by_type.get("Concept", 0),
            "nodes_by_type": n_by_type,
            "edges_by_type": e_by_type,
        }


def _node_dict(row: sqlite3.Row) -> dict:
    try:
        props = json.loads(row["properties_json"]) if row["properties_json"] else {}
    except Exception:
        props = {}
    return {
        "id": row["id"], "type": row["type"], "label": row["label"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "properties": props,
    }


def _edge_dict(row: sqlite3.Row) -> dict:
    try:
        props = json.loads(row["properties_json"]) if row["properties_json"] else {}
    except Exception:
        props = {}
    return {
        "id": row["id"], "src": row["src_id"], "dst": row["dst_id"],
        "type": row["type"], "properties": props,
    }


# ─────────────────────────── concept extraction ───────────────────────────

def extract_concepts(text: str) -> list[str]:
    """Canonical concept labels that appear in `text` (deterministic vocab match)."""
    if not text:
        return []
    concepts = [label for label, rx in _COMPILED_VOCAB.items() if rx.search(text)]
    # Practice concepts use the exact same bounded matcher as execution
    # preflights, so graph projection cannot drift into broader substring
    # matches or invent authority.
    matched = _practices.match_practices(text, "orchestrator")
    concepts.extend(item["practice_id"] for item in matched.get("matches", []))
    return list(dict.fromkeys(concepts))


def _wikilinks(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text or "")


# ─────────────────────────── ingestion ───────────────────────────

def ingest_tasks(store: GraphStore) -> dict:
    """Kanban tasks → Task + Project nodes, PART_OF (task→project) and
    ASSIGNED_TO (task→agent) edges."""
    from . import db
    counts = {"tasks": 0, "projects": 0, "edges": 0}
    try:
        proj_map = db._projects_map()  # {project_id: {name, color, icon}}
    except Exception:
        proj_map = {}
    proj_nodes = {}
    for pid, meta in (proj_map or {}).items():
        meta = meta if isinstance(meta, dict) else {}
        plabel = meta.get("name") or str(pid)
        nid = store.add_node("Project", plabel, node_id=f"project:{_slug(str(pid))}",
                             properties={"project_id": pid, "color": meta.get("color"),
                                         "icon": meta.get("icon")})
        proj_nodes[pid] = nid
        counts["projects"] += 1

    for t in db.get_all_tasks():
        d = t.to_dict()
        tid = store.add_node("Task", d.get("title") or d["id"][:8],
                             node_id=f"task:{d['id']}",
                             properties={"status": d.get("status"), "assignee": d.get("assignee"),
                                         "priority": d.get("priority"), "kanban_id": d["id"]},
                             created_at=d.get("created_at"))
        counts["tasks"] += 1
        pid = d.get("project_id")
        if pid:
            if pid not in proj_nodes:
                proj_nodes[pid] = store.add_node("Project", str(pid), node_id=f"project:{_slug(str(pid))}")
                counts["projects"] += 1
            store.add_edge(tid, proj_nodes[pid], "PART_OF")
            counts["edges"] += 1
        assignee = d.get("assignee")
        if assignee:
            aid = store.add_node("Agent", assignee, node_id=f"agent:{_slug(assignee)}",
                                 properties={"kind": d.get("assignee_type")})
            store.add_edge(tid, aid, "ASSIGNED_TO")
            counts["edges"] += 1
    return counts


def ingest_git(store: GraphStore, repo: Path = REPO_DIR, limit: int = 200) -> dict:
    """git log → Commit + Agent nodes, AUTHORED_BY (commit→agent) edges. Commit
    messages are also scanned for concepts → MENTIONS edges."""
    counts = {"commits": 0, "agents": 0, "edges": 0}
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"-{limit}",
             "--pretty=format:%H%x1f%an%x1f%at%x1f%s"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return counts
    seen_agents = set()
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, author, ts, subject = parts
        cid = store.add_node("Commit", f"{sha[:7]} {subject}"[:80],
                             node_id=f"commit:{sha[:12]}",
                             properties={"sha": sha, "subject": subject, "author": author},
                             created_at=int(ts) if ts.isdigit() else None)
        counts["commits"] += 1
        aid = store.add_node("Agent", author, node_id=f"agent:{_slug(author)}",
                             properties={"kind": "human" if "ricardo" in author.lower() else "agent"})
        if aid not in seen_agents:
            seen_agents.add(aid)
            counts["agents"] += 1
        store.add_edge(cid, aid, "AUTHORED_BY")
        counts["edges"] += 1
        for concept in extract_concepts(subject):
            kid = store.add_node("Concept", concept, node_id=f"concept:{_slug(concept)}")
            store.add_edge(cid, kid, "MENTIONS")
            counts["edges"] += 1
    return counts


def ingest_notes(store: GraphStore, knowledge_dir: Path = KNOWLEDGE_DIR) -> dict:
    """Research files in knowledge/*.md → Note nodes, MENTIONS (note→concept)
    and LINKS_TO (note→note) edges (Obsidian [[wikilinks]] + relative paths)."""
    counts = {"notes": 0, "concepts": 0, "edges": 0}
    if not knowledge_dir.exists():
        return counts
    files = sorted(knowledge_dir.glob("*.md"))
    concept_ids = {}
    note_by_stem = {}
    # First pass: create Note nodes + concept edges.
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # Title = first markdown H1, else filename.
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = (m.group(1).strip() if m else f.stem)[:100]
        nid = store.add_node("Note", title, node_id=f"note:{f.stem}",
                             properties={"file": f.name, "chars": len(text)},
                             created_at=int(f.stat().st_mtime))
        note_by_stem[f.stem] = nid
        note_by_stem[f.name] = nid
        counts["notes"] += 1
        for concept in extract_concepts(text):
            if concept not in concept_ids:
                concept_ids[concept] = store.add_node("Concept", concept,
                                                      node_id=f"concept:{_slug(concept)}")
                counts["concepts"] += 1
            store.add_edge(nid, concept_ids[concept], "MENTIONS")
            counts["edges"] += 1
    # Second pass: LINKS_TO between notes (wikilinks + `knowledge/foo.md` refs).
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        src = note_by_stem.get(f.stem)
        targets = set(_wikilinks(text))
        targets |= set(re.findall(r"(?:knowledge/)?([A-Za-z0-9_-]+)\.md", text))
        for tgt in targets:
            tgt_stem = tgt.replace(".md", "")
            dst = note_by_stem.get(tgt_stem) or note_by_stem.get(tgt_stem + ".md")
            if dst and dst != src:
                store.add_edge(src, dst, "LINKS_TO")
                counts["edges"] += 1
    return counts


# Phase 1 metadata prefix regex (module-level, CRLF-tolerant — Kimi review fix)
_METADATA_RE_GRAPH = re.compile(
    r"^\[src:([^\]|]+)\s*\|\s*imp:([1-5])\s*\|\s*sens:(normal|sensitive)\]\r?\n",
    re.MULTILINE,
)


def ingest_memory(store: GraphStore, memory_md: Path = MEMORY_MD) -> dict:
    """MEMORY.md entries (split on §) → Decision nodes. A decision that cites a
    research file path gets a DERIVED_FROM (decision→note) edge; decisions also
    MENTION concepts.

    Phase 1 upgrade: entries may now carry a metadata prefix line
    ``[src:... | imp:1-5 | sens:normal|sensitive]`` which is parsed and stored
    as node properties (source, importance, sensitivity).
    """
    counts = {"decisions": 0, "edges": 0, "evolved": 0}
    if not memory_md.exists():
        return counts
    try:
        raw = memory_md.read_text(errors="replace")
    except Exception:
        return counts
    for seg in raw.split("§"):
        seg = seg.strip()
        if not seg:
            continue
        # Phase 1: extract metadata prefix if present
        meta = {"source": "unknown", "importance": 1, "sensitivity": "normal"}
        m = _METADATA_RE_GRAPH.match(seg)
        if m:
            meta = {
                "source": m.group(1).strip(),
                "importance": int(m.group(2)),
                "sensitivity": m.group(3),
            }
            seg = seg[m.end():]
        # Label: leading [TAG] if present, else first ~60 chars.
        tag = re.match(r"\[([A-Z]+)\]", seg)
        label = (seg[:70] + "…") if len(seg) > 70 else seg
        props = {"tag": tag.group(1) if tag else None, "text": seg}
        props.update(meta)  # Phase 1: merge metadata into properties
        node_id = f"decision:{_hash(seg)}"
        # Is this a genuinely new fact? Only new facts trigger evolution, so an
        # idempotent re-ingest doesn't re-evolve (and re-stamp) the whole graph.
        is_new = store.get_node(node_id) is None
        did = store.add_node("Decision", label, node_id=node_id, properties=props)
        counts["decisions"] += 1
        # DERIVED_FROM: reference to a knowledge/*.md file.
        for ref in re.findall(r"([A-Za-z0-9_-]+)\.md", seg):
            note_id = f"note:{ref}"
            if store.get_node(note_id):
                store.add_edge(did, note_id, "DERIVED_FROM")
                counts["edges"] += 1
        for concept in extract_concepts(seg):
            kid = store.add_node("Concept", concept, node_id=f"concept:{_slug(concept)}")
            store.add_edge(did, kid, "MENTIONS")
            counts["edges"] += 1
        # Phase 2 — A-MEM memory evolution (GLM review fix, MED): after a NEW
        # fact lands, revisit related existing Decision nodes and evolve their
        # metadata (record the back-link + re-stamp last_verified). This is the
        # only caller wiring find_related + evolve_node into the write path.
        if is_new:
            counts["evolved"] += _evolve_related_decisions(store, did, seg)
    return counts


def _evolve_related_decisions(store: GraphStore, new_id: str, text: str,
                              limit: int = 3) -> int:
    """A-MEM evolution helper: find existing Decision nodes related to a newly
    ingested fact (by shared concept/keyword) and evolve them — record a
    ``related_decisions`` back-link and draw a LINKS_TO edge. Returns the number
    of nodes evolved. Concept surface forms are the query terms because
    find_related matches on label containment (short queries, not full text)."""
    # Query terms: the fact's canonical concepts (they appear in sibling labels),
    # falling back to the leading [TAG] when no concept matched.
    terms = extract_concepts(text)
    tag = re.match(r"\[([A-Z]+)\]", text)
    if tag:
        terms.append(tag.group(1))
    evolved = 0
    seen: set = set()
    for term in terms:
        for r in find_related(term, store=store, limit=limit):
            rid = r["id"]
            if rid == new_id or r.get("type") != "Decision" or rid in seen:
                continue
            seen.add(rid)
            links = list(r["properties"].get("related_decisions") or [])
            if new_id in links:
                continue
            links.append(new_id)
            evolve_node(rid, {"related_decisions": links}, store=store)
            store.add_edge(new_id, rid, "LINKS_TO")
            evolved += 1
    return evolved


def ingest_sprints(store: GraphStore) -> dict:
    """sprints → Sprint nodes; the task_sprints commit-ledger → PART_OF edges
    (task→sprint, outcome carried as an edge property). Closed cycles become
    reconstructable memory, not just rows."""
    from . import db
    counts = {"sprints": 0, "edges": 0}
    conn = db.get_conn()
    try:
        for s in conn.execute("SELECT * FROM sprints").fetchall():
            sid = store.add_node(
                "Sprint", s["name"], node_id=f"sprint:{s['id']}",
                properties={"sprint_id": s["id"], "status": s["status"],
                            "start_date": s["start_date"], "end_date": s["end_date"],
                            "project_id": s["project_id"]},
                created_at=s["created_at"])
            counts["sprints"] += 1
            if s["project_id"]:
                store.add_edge(sid, f"project:{_slug(str(s['project_id']))}", "PART_OF")
                counts["edges"] += 1
        try:
            ledger = conn.execute(
                "SELECT task_id, sprint_id, outcome FROM task_sprints").fetchall()
        except sqlite3.OperationalError:
            ledger = []
        for row in ledger:
            store.add_edge(f"task:{row['task_id']}", f"sprint:{row['sprint_id']}",
                           "PART_OF", properties={"outcome": row["outcome"]})
            counts["edges"] += 1
    finally:
        conn.close()
    return counts


def ingest_ledger(store: GraphStore) -> dict:
    """task_ledger (the verification record) → RESULTED_IN edges: each row
    becomes a Decision node (what the VALIDATE session concluded) linked from
    its task. Turns the write-only audit into recallable memory (Phase 5)."""
    from . import db
    counts = {"results": 0, "edges": 0}
    conn = db.get_conn()
    try:
        try:
            rows = conn.execute("SELECT * FROM task_ledger").fetchall()
        except sqlite3.OperationalError:
            return counts
        for r in rows:
            label = (r["summary"] or "verification")[:70]
            did = store.add_node(
                "Decision", label, node_id=f"ledger:{r['id']}",
                properties={"ledger_id": r["id"], "task_id": r["task_id"],
                            "agent": r["agent"], "role": r["role"],
                            "passed": bool(r["passed"]), "status": r["status"],
                            "store": "task_ledger"},
                created_at=r["created_at"])
            counts["results"] += 1
            if r["task_id"]:
                store.add_edge(f"task:{r['task_id']}", did, "RESULTED_IN")
                counts["edges"] += 1
    finally:
        conn.close()
    return counts


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


def ingest_cc_memory(store: GraphStore, root: Path = None) -> dict:
    """Claude Code auto-memory (~/.claude/projects/*/memory/*.md) → Decision
    nodes with provenance (file path, description, last_verified when stamped).
    [[wikilinks]] between memories become LINKS_TO. Read-only."""
    root = root or CC_MEMORY_ROOT
    counts = {"memories": 0, "edges": 0}
    if not root.exists():
        return counts
    by_name = {}
    files = sorted(root.glob("*/memory/*.md"))[:400]
    for f in files:
        if f.name == "MEMORY.md":       # the index, not a fact
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        name, desc, last_verified = f.stem, "", None
        m = _FRONTMATTER_RE.match(text)
        if m:
            fm = m.group(1)
            nm = re.search(r"^name:\s*(.+)$", fm, re.M)
            dm = re.search(r"^description:\s*(.+)$", fm, re.M)
            lv = re.search(r"^last_verified:\s*(.+)$", fm, re.M)
            if nm:
                name = nm.group(1).strip()
            if dm:
                desc = dm.group(1).strip()
            if lv:
                last_verified = lv.group(1).strip()
        nid = store.add_node(
            "Decision", name, node_id=f"ccmem:{_slug(f.parent.parent.name)}:{_slug(f.stem)}",
            properties={"store": "cc-memory", "file": str(f), "description": desc,
                        "last_verified": last_verified},
            created_at=int(f.stat().st_mtime))
        by_name[name] = nid
        by_name[f.stem] = nid
        counts["memories"] += 1
        for concept in extract_concepts(text):
            store.add_edge(nid, store.add_node("Concept", concept,
                           node_id=f"concept:{_slug(concept)}"), "MENTIONS")
            counts["edges"] += 1
    for f in files:
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        src = by_name.get(f.stem)
        if not src:
            continue
        for tgt in _wikilinks(text):
            dst = by_name.get(tgt.strip())
            if dst and dst != src:
                store.add_edge(src, dst, "LINKS_TO")
                counts["edges"] += 1
    return counts


def ingest_changelog(store: GraphStore, repo: Path = None, limit: int = 40) -> dict:
    """docs/changelog.md (the per-repo episodic log) → Note nodes, newest first.
    One node per `## <timestamp> — <tag>` entry; concepts MENTIONed."""
    repo = repo or REPO_DIR
    counts = {"entries": 0, "edges": 0}
    f = repo / "docs" / "changelog.md"
    if not f.exists():
        return counts
    try:
        raw = f.read_text(errors="replace")
    except Exception:
        return counts
    entries = re.split(r"^## ", raw, flags=re.M)[1:limit + 1]
    for seg in entries:
        header = seg.split("\n", 1)[0].strip()
        body = seg[:2000]
        nid = store.add_node(
            "Note", f"changelog: {header}"[:90],
            node_id=f"changelog:{_hash(header)}",
            properties={"store": "changelog", "file": str(f), "header": header})
        counts["entries"] += 1
        for concept in extract_concepts(body):
            store.add_edge(nid, store.add_node("Concept", concept,
                           node_id=f"concept:{_slug(concept)}"), "MENTIONS")
            counts["edges"] += 1
    return counts


def ingest_obsidian(store: GraphStore, vault: Path = None, cap: int = 500) -> dict:
    """Obsidian vault, READ-ONLY and titles-only (least disclosure: the graph
    stores labels + pointers, never content). Wikilinks → LINKS_TO."""
    vault = vault or OBSIDIAN_DIR
    counts = {"notes": 0, "edges": 0}
    if not vault.exists():
        return counts
    files = sorted(p for p in vault.rglob("*.md")
                   if ".obsidian" not in p.parts and ".trash" not in p.parts)[:cap]
    by_stem = {}
    for f in files:
        nid = store.add_node(
            "Note", f.stem, node_id=f"obsidian:{_slug(str(f.relative_to(vault)))}",
            properties={"store": "obsidian", "file": str(f)},
            created_at=int(f.stat().st_mtime))
        by_stem[f.stem] = nid
        counts["notes"] += 1
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        src = by_stem.get(f.stem)
        for tgt in _wikilinks(text):
            dst = by_stem.get(tgt.split("|")[0].split("#")[0].strip())
            if dst and dst != src:
                store.add_edge(src, dst, "LINKS_TO")
                counts["edges"] += 1
    return counts


def ingest_all(store: GraphStore = None, rebuild: bool = False) -> dict:
    """Run every ingester. `rebuild=True` wipes the graph first for a clean
    rebuild (ingestion is idempotent either way via stable IDs)."""
    store = store or GraphStore()
    if rebuild:
        store.clear()
    summary = {
        "notes": ingest_notes(store),   # notes first so DERIVED_FROM can resolve
        "tasks": ingest_tasks(store),
        "git": ingest_git(store),
        "memory": ingest_memory(store),
        # Phase 5 — the missing stores join the index:
        "sprints": ingest_sprints(store),
        "ledger": ingest_ledger(store),
        "cc_memory": ingest_cc_memory(store),
        "changelog": ingest_changelog(store),
        "obsidian": ingest_obsidian(store),
    }
    summary["stats"] = store.stats()
    return summary


# ─────────────────────────── recall (Phase 5 — THE one read path) ───────────
# recall(query) = graph search → 1-hop context → resolve pointer → read the
# AUTHORITATIVE source (the graph stores labels + pointers, never content) →
# return {fact, source, ref, staleness}. Both orchestrators call THIS instead
# of inventing their own federation over six stores (consolidation, not
# federation — the §4.3 doctrine).

# Staleness horizons (days) by node type — mirrors the memory-governance TTLs:
# past the horizon a fact is STALE → re-verify before trusting (never deleted).
_STALENESS_DAYS = {
    "Note": 30,        # reference material
    "Decision": 14,    # project-ish facts
    "Concept": None,   # derived vocabulary — no clock
    "Agent": None,
}


def _staleness(node: dict) -> str:
    """fresh | stale | n/a — deterministic, from timestamps only (the mem-audit
    doctrine: staleness is ground truth, never an LLM judgment)."""
    t = node.get("type")
    if t in ("Task", "Sprint", "Project"):
        return "fresh"       # resolved live from the authoritative store below
    if t == "Commit":
        return "fresh"       # immutable history
    horizon = _STALENESS_DAYS.get(t)
    if horizon is None:
        return "n/a"
    props = node.get("properties") or {}
    # cc-memory facts carry last_verified (the governance stamp) — prefer it.
    lv = props.get("last_verified")
    if lv:
        try:
            import datetime as _dt
            age = (_dt.date.today() - _dt.date.fromisoformat(str(lv)[:10])).days
            return "stale" if age > horizon else "fresh"
        except Exception:
            pass
    ts = node.get("updated_at") or node.get("created_at") or 0
    return "stale" if (_now() - ts) > horizon * 86400 else "fresh"


def _resolve_source(node: dict) -> dict:
    """Follow the node's pointer to its authoritative store and read the CURRENT
    fact there (live reads for DB-backed types; path + existence for files)."""
    t, props = node.get("type"), node.get("properties") or {}
    if t == "Task":
        from . import db
        task = db.get_task(props.get("kanban_id") or node["id"].split(":", 1)[1])
        if task:
            return {"source": "kanban.db", "ref": task.id,
                    "fact": f"{task.title} — status={task.status}, assignee={task.assignee}"}
        return {"source": "kanban.db", "ref": node["id"], "fact": node["label"] + " (task no longer exists)"}
    if t == "Sprint":
        from . import db
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT * FROM sprints WHERE id = ?",
                               (props.get("sprint_id"),)).fetchone()
        finally:
            conn.close()
        if row:
            return {"source": "kanban.db:sprints", "ref": row["id"],
                    "fact": f"{row['name']} — status={row['status']}"}
    if t == "Commit":
        return {"source": "git", "ref": props.get("sha", node["id"]),
                "fact": props.get("subject") or node["label"]}
    f = props.get("file")
    if f:
        p = Path(f)
        store_name = props.get("store") or ("knowledge" if "knowledge" in str(p) else "file")
        return {"source": store_name, "ref": str(p),
                "fact": (props.get("description") or node["label"]) +
                        ("" if p.exists() else " (FILE MISSING)")}
    if props.get("store") == "task_ledger":
        return {"source": "task_ledger", "ref": f"ledger:{props.get('ledger_id')}",
                "fact": f"{node['label']} — passed={props.get('passed')} by {props.get('agent')}"}
    if props.get("text"):
        return {"source": "MEMORY.md", "ref": str(MEMORY_MD), "fact": props["text"][:300]}
    return {"source": "graph (derived index)", "ref": node["id"], "fact": node["label"]}


# ─────────────────────────── Phase 2: contradiction + evolution ───────────

# Negation patterns for contradiction detection (keyword-based, not LLM).
# If the new fact contains a negation pattern that the existing fact does NOT,
# or vice versa, we flag a potential contradiction.
_NEGATION_PATTERNS = re.compile(
    r"\b(?:not|never|no longer|rejected|deprecated|replaced by|removed|"
    r"disabled|obsolete|abandoned|dropped|eliminated|stopped|cancelled|won't|"
    r"do not|don't|refuse|reject|avoid|prohibit|forbid)\b",
    re.IGNORECASE,
)


def contradiction_check(new_fact: str, existing_facts: list[str]) -> dict:
    """Keyword-based contradiction detection (MemClaw pattern: check BEFORE dedup).

    Returns ``{"contradicts": bool, "which": int | None, "reason": str}``.
    A contradiction is flagged when:
      - The new fact and an existing fact share significant word overlap (>40%)
        AND one contains a negation pattern the other lacks.
      - Or the new fact directly negates an existing fact (contains the existing
        fact's key terms preceded by a negation).

    This is intentionally conservative — false positives are better than silently
    dropping a real contradictory write (the #1 production bug per MemClaw).
    """
    new_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{3,}", new_fact))
    new_negated = bool(_NEGATION_PATTERNS.search(new_fact))

    for i, existing in enumerate(existing_facts):
        existing_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{3,}", existing))
        existing_negated = bool(_NEGATION_PATTERNS.search(existing))

        # Need word overlap to compare the two facts.
        overlap = new_words & existing_words
        if not overlap:
            continue
        # Jaccard similarity
        union = new_words | existing_words
        sim = len(overlap) / len(union) if union else 0
        # Minimum-overlap gate (GLM review fix): a bare 0.10 Jaccard fires on a
        # single shared noun and produces false positives. Require either 2+
        # shared words OR a strong single-word similarity (>=0.25) before we
        # treat two facts as being about the same subject.
        if len(overlap) < 2 and sim < 0.25:
            continue

        # Contradiction signal: one is negated, the other isn't, and they share
        # enough words to be about the same subject.
        if new_negated != existing_negated:
            return {
                "contradicts": True,
                "which": i,
                "reason": (
                    f"New fact {'negates' if new_negated else 'affirms'} what "
                    f"existing fact #{i} {'affirms' if not existing_negated else 'negates'} "
                    f"(overlap={sim:.0%}, shared: {', '.join(sorted(list(overlap))[:5])})"
                ),
            }
    return {"contradicts": False, "which": None, "reason": ""}


def find_related(query: str, store: "GraphStore" = None, limit: int = 5) -> list[dict]:
    """Find graph nodes related to a query by label similarity.
    Used by memory evolution: after writing a new fact, find existing nodes
    that might need their metadata updated (A-MEM memory evolution pattern).
    Note: scores by label word overlap only (edges not consulted — Kimi review).
    """
    store = store or get_store()
    # Search by label substring
    matches = store.search(query, limit=limit * 2)
    if not matches:
        return []
    # Score by how many query words appear in the label. Tokenize with the same
    # regex contradiction_check uses (>=3-letter alpha words) so scoring is
    # consistent across the two paths and ignores punctuation/short noise words
    # (GLM review LOW fix — was a naive whitespace split).
    query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))
    scored = []
    for m in matches:
        label_words = set(re.findall(r"[a-zA-Z]{3,}", m["label"].lower()))
        score = len(query_words & label_words) / max(len(query_words), 1)
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]


def evolve_node(node_id: str, new_properties: dict, store: "GraphStore" = None) -> bool:
    """Update a node's properties (A-MEM memory evolution: after inserting a
    new fact, revisit related historical memories and update their metadata).

    Merges ``new_properties`` into the existing ``properties`` dict.
    Returns True if the node was found and updated.
    """
    store = store or get_store()
    node = store.get_node(node_id)
    if not node:
        return False
    props = node.get("properties", {})
    props.update(new_properties)
    props["last_evolved_at"] = _now()
    # Also bump last_verified (ISO date string — matches cc-memory format
    # that _staleness() expects via date.fromisoformat). GLM review fix #1.
    import datetime
    props["last_verified"] = datetime.date.today().isoformat()
    store.add_node(
        node["type"], node["label"], node_id=node_id,
        properties=props, created_at=node.get("created_at"),
    )
    return True


def archive_node(node_id: str, reason: str, store: "GraphStore" = None) -> bool:
    """Archive a node (decay activation): set ``status=archived`` in properties.
    The node stays in the DB (queryable but not in the active set). Never deleted.
    Preserves the original status in ``original_status`` so it can be restored.
    Returns True if the node was found and archived.
    """
    store = store or get_store()
    node = store.get_node(node_id)
    if not node:
        return False
    props = node.get("properties", {})
    if props.get("status") == "archived":
        return True  # Already archived, idempotent
    # Preserve original status before overwriting (HIGH fix from Kimi review)
    original = props.get("status")
    if original and original != "archived":
        props["original_status"] = original
    props["status"] = "archived"
    props["archived_at"] = _now()
    props["archive_reason"] = reason
    store.add_node(
        node["type"], node["label"], node_id=node_id,
        properties=props, created_at=node.get("created_at"),
    )
    return True


# Staleness TTLs by node type (days) — mirrors memory-governance TTLs.
_TTL_BY_TYPE = {
    "Decision": 180,      # decisions are long-lived
    "Note": 30,           # research notes go stale faster
    # Task removed — live DB records, shouldn't be archived by age (Kimi review)
    "Concept": 365,       # concepts are evergreen
    "Commit": 90,         # old commits are reference-only
    "Session": 30,        # old sessions are rarely needed
    "Project": 365,       # projects are evergreen
    "Sprint": 30,         # past sprints are reference
    "Skill": 365,         # skills are evergreen
    "Agent": 365,         # agents are evergreen
}


def _last_meaningful_ts(nd: dict, birth_fallback: int) -> int:
    """Age basis for archival: a node's BIRTH extended only by GENUINE activity.

    NOT ``updated_at`` — that is a last-WRITE timestamp bumped by every bulk
    re-ingest / migration (reads via ``recall()`` never touch it), so a single
    rebuild resets the whole graph's staleness clock (the observed "0 archived /
    617 active" freeze: one 2026-07-08 rebuild set updated_at on all 617 nodes).
    Bitemporal (memory-governance doctrine): separate transaction-time
    (updated_at, noisy) from meaningful activity. The real "don't archive a node
    we genuinely re-touched" signals are the ones ``evolve_node`` writes:
    ``last_evolved_at`` (epoch, A-MEM evolution) and ``last_verified`` (ISO date,
    governance re-verify). Age from ``created_at``, pushed forward by those only.
    """
    props = nd.get("properties") or {}
    ts = nd.get("created_at") or birth_fallback
    le = props.get("last_evolved_at")
    if isinstance(le, (int, float)) and le > ts:
        ts = int(le)
    lv = props.get("last_verified")
    if lv:
        try:
            import datetime as _dt
            lv_ts = int(_dt.datetime.combine(
                _dt.date.fromisoformat(str(lv)[:10]), _dt.time()).timestamp())
            if lv_ts > ts:
                ts = lv_ts
        except Exception:
            pass
    return ts


def get_stale_nodes(ttl_override: dict = None, store: "GraphStore" = None) -> list[dict]:
    """Nodes older than their type's TTL, aged from BIRTH (``created_at``) and
    pushed forward ONLY by genuine activity (``last_evolved_at`` /
    ``last_verified``) — never by the migration-polluted ``updated_at`` column
    (see ``_last_meaningful_ts``). Returns node dicts with a ``ttl_days`` field.
    """
    store = store or get_store()
    ttls = {**_TTL_BY_TYPE, **(ttl_override or {})}
    now = _now()
    stale = []
    with store._conn() as c:
        rows = c.execute("SELECT * FROM nodes WHERE type IN ({})".format(
            ",".join("?" * len(ttls))
        ), list(ttls.keys())).fetchall()
    for r in rows:
        nd = _node_dict(r)
        # Skip already-archived nodes
        if nd["properties"].get("status") == "archived":
            continue
        ttl_s = ttls.get(nd["type"], 30) * 86400
        effective_age = now - _last_meaningful_ts(nd, now)
        if effective_age > ttl_s:
            nd["ttl_days"] = ttls.get(nd["type"], 30)
            nd["age_days"] = effective_age // 86400
            stale.append(nd)
    return stale


def archive_stale(ttl_override: dict = None, store: "GraphStore" = None) -> int:
    """Archive all stale nodes. Returns count archived."""
    store = store or get_store()
    stale = get_stale_nodes(ttl_override=ttl_override, store=store)
    count = 0
    for nd in stale:
        reason = f"TTL expired ({nd['ttl_days']}d, age={nd['age_days']}d)"
        if archive_node(nd["id"], reason, store=store):
            count += 1
    return count


# ─────────────────────────── Phase 3: metabolism metrics ───────────────────

def get_metabolism_stats(store: "GraphStore" = None) -> dict:
    """Memory metabolism metrics (arXiv:2604.12034): the digestive system view.

    Returns counts for the last 24h:
      - inputs_processed: nodes created in last 24h
      - facts_distilled: nodes updated in last 24h (re-ingested = refined)
      - memories_evicted: nodes archived in last 24h
      - decay_triggered: nodes past TTL (stale but not yet archived)
      - total_active: active (non-archived) nodes
      - total_archived: archived nodes
    """
    store = store or get_store()
    cutoff = _now() - 86400  # 24h ago
    # GLM review fix: aggregate archived/active counts in SQL via json_extract
    # (the same pattern search()/all_graph() use) instead of a Python loop over
    # every node — O(1) queries with an index-friendly plan, not O(N) in Python.
    _NOT_ARCHIVED = ("(properties_json IS NULL "
                     "OR json_extract(properties_json, '$.status') IS NULL "
                     "OR json_extract(properties_json, '$.status') != 'archived')")
    with store._conn() as c:
        inputs = c.execute(
            "SELECT COUNT(*) n FROM nodes WHERE created_at >= ?", (cutoff,)
        ).fetchone()["n"]
        distilled = c.execute(
            "SELECT COUNT(*) n FROM nodes WHERE updated_at >= ? AND created_at < ?",
            (cutoff, cutoff),
        ).fetchone()["n"]
        archived_total = c.execute(
            "SELECT COUNT(*) n FROM nodes "
            "WHERE json_extract(properties_json, '$.status') = 'archived'"
        ).fetchone()["n"]
        archived_24h = c.execute(
            "SELECT COUNT(*) n FROM nodes "
            "WHERE json_extract(properties_json, '$.status') = 'archived' "
            "AND CAST(json_extract(properties_json, '$.archived_at') AS INTEGER) >= ?",
            (cutoff,),
        ).fetchone()["n"]
        active_total = c.execute(
            f"SELECT COUNT(*) n FROM nodes WHERE {_NOT_ARCHIVED}"
        ).fetchone()["n"]
    # Stale = past TTL but not archived
    stale = len(get_stale_nodes(store=store))
    return {
        "inputs_processed": inputs,
        "facts_distilled": distilled,
        "memories_evicted": archived_24h,
        "decay_triggered": stale,
        "total_active": active_total,
        "total_archived": archived_total,
    }


def recall(query: str, project_id: str = None, task_id: str = None, k: int = 8) -> dict:
    """The unified memory read (§4.3): search the join-index, resolve each hit
    to its authoritative source, flag staleness. With task_id, the task's 1-hop
    neighborhood is prepended (its project/sprint/ledger context)."""
    store = get_store()
    results, seen = [], set()

    def _emit(node, why):
        if node["id"] in seen or len(results) >= k:
            return
        seen.add(node["id"])
        src = _resolve_source(node)
        results.append({"id": node["id"], "type": node["type"], "label": node["label"],
                        "why": why, "staleness": _staleness(node), **src})

    if task_id:
        for nb in store.neighbors(f"task:{task_id}"):
            _emit(nb, "task-context")
    matches = store.search(query, limit=k * 3)
    if project_id:
        proj_node = f"project:{_slug(str(project_id))}"
        in_proj = {n["id"] for n in store.neighbors(proj_node)}
        matches.sort(key=lambda n: n["id"] not in in_proj)   # project-linked first
    for m in matches:
        _emit(m, "match")
    return {"query": query, "k": k, "count": len(results), "results": results}


# ─────────────── module-level convenience wrappers ───────────────

_DEFAULT_STORE = None


def get_store() -> GraphStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = GraphStore()
    return _DEFAULT_STORE


def search_nodes(query: str, type: str = None) -> list[dict]:
    return get_store().search(query, type=type)


def expand(node_id: str, hops: int = 2) -> dict:
    return get_store().expand(node_id, hops=hops)


def export_semantica_projection(store: GraphStore = None) -> dict:
    """Return the privacy-bounded, rebuildable Semantica input projection.

    The raw graph database and governed memory bodies never cross the service
    boundary.  Keeping the policy in ``sanitize_projection`` also means the
    host export and container import enforce the same schema independently.
    """
    from semantica_service import sanitize_projection

    store = store or get_store()
    # Read one sentinel beyond the service cap: requesting exactly 5,000 would
    # silently truncate a 5,001-node canonical graph before the sanitizer can
    # refuse it. Overflow must fail the build, never look like a valid subset.
    graph = store.all_graph(cap=5001, include_archived=False)
    # all_graph already exposes properties as a parsed object.  The sanitizer
    # drops every field outside the explicit metadata allowlist and refuses
    # secret-like content before anything is written to the service input dir.
    return sanitize_projection(graph)


if __name__ == "__main__":
    import sys
    st = GraphStore()
    do_rebuild = "--rebuild" in sys.argv or "--fresh" in sys.argv
    result = ingest_all(st, rebuild=do_rebuild)
    print(json.dumps(result, indent=2, ensure_ascii=False))
