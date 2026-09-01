#!/usr/bin/env python3
"""Bounded Semantica projection service for Hermes.

Hermes's SQLite graph is canonical.  This process accepts only a sanitized
export, builds an atomic derived snapshot, and uses Semantica's provenance
manager to attach source lineage.  Deleting /data is always recoverable by a
rebuild from Hermes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import socketserver
from collections import deque
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_NODES = 5_000
MAX_EDGES = 20_000
MAX_QUERY = 512
MAX_HOPS = 2
MAX_RESULT_NODES = 50
MAX_RESULT_EDGES = 100
MAX_K = 10
MAX_RESPONSE_BYTES = 8 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
NODE_BATCH = 200
EDGE_BATCH = 500
PREVIOUS_MAX_AGE = 24 * 60 * 60
MAINTENANCE_INTERVAL = 60
STALE_FACT_AGE = 14 * 24 * 60 * 60
ALLOWED_NODE_TYPES = {
    "Project", "Task", "Decision", "Note", "Session", "Concept", "Agent",
    "Skill", "Commit", "Initiative", "Sprint",
}
ALLOWED_EDGE_TYPES = {
    "MENTIONS", "PART_OF", "DECIDED_IN", "RESULTED_IN", "DERIVED_FROM",
    "IMPLEMENTS", "ASSIGNED_TO", "DEPENDS_ON", "AUTHORED_BY", "LINKS_TO",
}
ALLOWED_PROPERTIES = {
    "status", "last_verified", "store", "source", "ref", "sha",
    "initiative_id", "sprint_id", "kanban_id", "project_id",
}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}", re.I)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|bearer|password|secret)"
    r"\s*[:=]\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9_-]{16,}"
)
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s]+/")


class ProjectionError(ValueError):
    pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def sanitize_label(value: object, node_type: str) -> str:
    label = " ".join(str(value or "").split())[:240]
    label = _EMAIL.sub("[email]", label)
    label = _PHONE.sub("[phone]", label)
    label = _HOME_PATH.sub("<home>/", label)
    label = _SECRET.sub("[redacted]", label)
    if not label:
        return f"{node_type} {_hash(str(value))}"
    return label[:120]


def _safe_property(key: str, value: object) -> object | None:
    if key not in ALLOWED_PROPERTIES or isinstance(value, (dict, list)):
        return None
    clean = " ".join(str(value or "").split())[:160]
    clean = _EMAIL.sub("[email]", clean)
    clean = _PHONE.sub("[phone]", clean)
    clean = _HOME_PATH.sub("<home>/", clean)
    clean = _SECRET.sub("[redacted]", clean)
    return clean


def sanitize_projection(raw: dict) -> dict:
    """Apply the privacy schema and reject malformed/oversized projections."""
    if not isinstance(raw, dict):
        raise ProjectionError("projection must be an object")
    raw_nodes, raw_edges = raw.get("nodes", []), raw.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ProjectionError("nodes and edges must be arrays")
    if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
        raise ProjectionError(
            f"projection exceeds caps ({len(raw_nodes)}/{MAX_NODES} nodes, "
            f"{len(raw_edges)}/{MAX_EDGES} edges)"
        )

    nodes = []
    seen = set()
    for offset in range(0, len(raw_nodes), NODE_BATCH):
        for item in raw_nodes[offset:offset + NODE_BATCH]:
            if not isinstance(item, dict):
                raise ProjectionError("node must be an object")
            nid, ntype = str(item.get("id", ""))[:160], str(item.get("type", ""))
            if not nid or ntype not in ALLOWED_NODE_TYPES or nid in seen:
                raise ProjectionError(f"invalid or duplicate node: {nid!r}/{ntype!r}")
            if _SECRET.search(json.dumps(item, ensure_ascii=False)):
                raise ProjectionError(f"secret-like content refused in node {nid}")
            props = {}
            for key, value in (item.get("properties") or {}).items():
                clean = _safe_property(str(key), value)
                if clean is not None:
                    props[str(key)] = clean
            nodes.append({
                "id": nid,
                "type": ntype,
                "label": sanitize_label(item.get("label"), ntype),
                "created_at": int(item.get("created_at") or 0),
                "updated_at": int(item.get("updated_at") or 0),
                "properties": props,
            })
            seen.add(nid)

    edges = []
    edge_seen = set()
    for offset in range(0, len(raw_edges), EDGE_BATCH):
        for item in raw_edges[offset:offset + EDGE_BATCH]:
            if not isinstance(item, dict):
                raise ProjectionError("edge must be an object")
            src, dst, etype = str(item.get("src", "")), str(item.get("dst", "")), str(item.get("type", ""))
            if src not in seen or dst not in seen or etype not in ALLOWED_EDGE_TYPES:
                raise ProjectionError(f"invalid edge {src!r}-{etype!r}->{dst!r}")
            eid = str(item.get("id") or _hash(f"{src}\x1f{etype}\x1f{dst}"))[:160]
            if eid in edge_seen:
                continue
            edges.append({"id": eid, "src": src, "dst": dst, "type": etype})
            edge_seen.add(eid)
    return {"schema": 1, "nodes": nodes, "edges": edges}


def _atomic_json(path: Path, value: dict) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ProjectionError(
            f"derived snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_projection_shacl(projection: dict) -> dict:
    """Validate the derived graph with Semantica's declared SHACL engine.

    Semantica 0.6.5's public SHACL wrapper delegates to pySHACL, but importing
    its ontology package eagerly imports the full ML distribution.  This
    bounded adapter calls the same declared engine directly and keeps only the
    RDF/SHACL dependencies in the no-network sidecar.
    """
    from pyshacl import validate
    from rdflib import BNode, Graph, Literal, Namespace, RDF, RDFS, XSD
    from rdflib.collection import Collection

    ex = Namespace("urn:hermes:projection:")
    sh = Namespace("http://www.w3.org/ns/shacl#")
    data, shapes = Graph(), Graph()

    node_shape = ex.HermesNodeShape
    shapes.add((node_shape, sh.targetClass, ex.HermesNode))
    edge_shape = ex.HermesEdgeShape
    shapes.add((edge_shape, sh.targetClass, ex.HermesEdge))

    def property_shape(owner, path, datatype=None, allowed=None):
        prop = BNode()
        shapes.add((owner, sh.property, prop))
        shapes.add((prop, sh.path, path))
        shapes.add((prop, sh.minCount, Literal(1)))
        shapes.add((prop, sh.maxCount, Literal(1)))
        if datatype is not None:
            shapes.add((prop, sh.datatype, datatype))
            if datatype == XSD.string:
                shapes.add((prop, sh.minLength, Literal(1)))
        if allowed is not None:
            head = BNode()
            Collection(shapes, head, [Literal(value) for value in sorted(allowed)])
            shapes.add((prop, sh["in"], head))

    property_shape(node_shape, ex.identifier, XSD.string)
    property_shape(node_shape, ex.nodeType, XSD.string, ALLOWED_NODE_TYPES)
    property_shape(node_shape, RDFS.label, XSD.string)
    property_shape(node_shape, ex.createdAt, XSD.integer)
    property_shape(node_shape, ex.updatedAt, XSD.integer)
    property_shape(edge_shape, ex.identifier, XSD.string)
    property_shape(edge_shape, ex.edgeType, XSD.string, ALLOWED_EDGE_TYPES)
    property_shape(edge_shape, ex.source, XSD.string)
    property_shape(edge_shape, ex.target, XSD.string)

    for node in projection.get("nodes", []):
        subject = ex["node-" + _hash(str(node.get("id", "")))]
        data.add((subject, RDF.type, ex.HermesNode))
        data.add((subject, ex.identifier, Literal(node.get("id"))))
        data.add((subject, ex.nodeType, Literal(node.get("type"))))
        if "label" in node:
            data.add((subject, RDFS.label, Literal(node.get("label"))))
        data.add((subject, ex.createdAt, Literal(node.get("created_at"), datatype=XSD.integer)))
        data.add((subject, ex.updatedAt, Literal(node.get("updated_at"), datatype=XSD.integer)))
    for edge in projection.get("edges", []):
        subject = ex["edge-" + _hash(str(edge.get("id", "")))]
        data.add((subject, RDF.type, ex.HermesEdge))
        data.add((subject, ex.identifier, Literal(edge.get("id"))))
        data.add((subject, ex.edgeType, Literal(edge.get("type"))))
        data.add((subject, ex.source, Literal(edge.get("src"))))
        data.add((subject, ex.target, Literal(edge.get("dst"))))

    conforms, results_graph, _ = validate(
        data, shacl_graph=shapes, inference="none"
    )
    violations = sum(1 for _ in results_graph.subjects(RDF.type, sh.ValidationResult))
    report = {
        "standard": "W3C SHACL",
        "engine": "pySHACL-0.30.1 (Semantica 0.6.5 contract)",
        "conforms": bool(conforms),
        "violations": violations,
    }
    if not conforms:
        raise ProjectionError(f"SHACL validation failed ({violations} violations)")
    return report


def projection_signals(projection: dict, as_of: int) -> dict:
    """Return bounded temporal/supersede/conflict evidence, never fact bodies."""
    superseded, stale = [], []
    refs = defaultdict(list)
    for node in projection.get("nodes", []):
        status = str(node.get("properties", {}).get("status", "")).lower()
        if status in {"superseded", "obsolete", "archived"}:
            superseded.append(node["id"])
        updated = int(node.get("updated_at") or 0)
        if updated and as_of - updated > STALE_FACT_AGE:
            stale.append(node["id"])
        ref = str(node.get("properties", {}).get("ref", ""))
        if ref:
            refs[ref].append(node)

    conflicts = []
    for ref, nodes in sorted(refs.items()):
        statuses = {str(n.get("properties", {}).get("status", "")) for n in nodes}
        labels = {str(n.get("label", "")).casefold() for n in nodes}
        if len(nodes) > 1 and (len(statuses) > 1 or len(labels) > 1):
            conflicts.append({
                "ref_hash": _hash(ref),
                "node_ids": sorted(n["id"] for n in nodes)[:10],
            })
    return {
        "as_of": as_of,
        "superseded_node_ids": sorted(superseded)[:10],
        "stale_node_ids": sorted(stale)[:10],
        "conflicts": conflicts[:10],
    }


def signals_for_nodes(signals: dict, node_ids: set[str]) -> dict:
    """Return only signals whose references resolve inside one query packet."""
    conflicts = []
    for conflict in signals.get("conflicts", []):
        kept = [node_id for node_id in conflict.get("node_ids", [])
                if node_id in node_ids]
        # A one-sided conflict is not evidence; include the signal only when
        # the packet contains at least two conflicting nodes.
        if len(kept) >= 2:
            conflicts.append({
                "ref_hash": conflict.get("ref_hash"),
                "node_ids": kept,
            })
    return {
        "as_of": signals.get("as_of"),
        "superseded_node_ids": [
            node_id for node_id in signals.get("superseded_node_ids", [])
            if node_id in node_ids
        ],
        "stale_node_ids": [
            node_id for node_id in signals.get("stale_node_ids", [])
            if node_id in node_ids
        ],
        "conflicts": conflicts,
    }


class ProjectionStore:
    def __init__(self, data_dir: Path, use_semantica: bool = True):
        self.data_dir = Path(data_dir)
        self.current_path = self.data_dir / "current.json"
        self.previous_path = self.data_dir / "previous.json"
        self.use_semantica = use_semantica
        self._lock = threading.RLock()
        self._prune_previous()
        self.snapshot = self._load_current()
        self.index = {}
        self.adj = {}
        self._index()

    def _prune_previous(self) -> None:
        try:
            if time.time() - self.previous_path.stat().st_mtime >= PREVIOUS_MAX_AGE:
                self.previous_path.unlink()
        except FileNotFoundError:
            pass

    def prune_expired_previous(self) -> None:
        """Enforce retention without turning a read endpoint into a writer."""
        with self._lock:
            self._prune_previous()

    def _load_current(self) -> dict:
        for path in (self.current_path, self.previous_path):
            try:
                value = json.loads(path.read_text())
                if isinstance(value, dict) and value.get("schema") == 1:
                    return value
            except Exception:
                continue
        return {"schema": 1, "snapshot": None, "built_at": None, "nodes": [], "edges": []}

    def _index(self) -> None:
        self.index = {n["id"]: n for n in self.snapshot.get("nodes", [])}
        self.adj = {nid: [] for nid in self.index}
        for e in self.snapshot.get("edges", []):
            if e["src"] in self.adj and e["dst"] in self.adj:
                self.adj[e["src"]].append((e, e["dst"]))
                self.adj[e["dst"]].append((e, e["src"]))

    def _attach_provenance(self, projection: dict) -> None:
        if not self.use_semantica:
            for node in projection["nodes"]:
                node["provenance"] = {"source": "hermes", "ref": node["id"]}
            return
        from semantica import __version__ as semantica_version
        from semantica.provenance import ProvenanceManager

        manager = ProvenanceManager()
        for node in projection["nodes"]:
            ref = node["properties"].get("ref") or node["id"]
            manager.track_entity(
                node["id"], source=f"hermes:{ref}",
                metadata={"projection": "hermes", "node_type": node["type"]},
                agent_id="hermes-semantica-projection",
            )
            lineage = manager.get_lineage(node["id"])
            node["provenance"] = {
                "standard": "W3C PROV-O",
                "engine": f"semantica-{semantica_version}",
                "source": (lineage.get("source_documents") or [f"hermes:{ref}"])[0],
                "ref": str(ref)[:160],
            }

    def rebuild(self, raw: dict) -> dict:
        started = time.monotonic()
        projection = sanitize_projection(raw)
        self._attach_provenance(projection)
        if self.use_semantica:
            projection["validation"] = validate_projection_shacl(projection)
        body_hash = hashlib.sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        built_at = int(time.time())
        projection.update({
            "snapshot": body_hash,
            "built_at": built_at,
            "source": "hermes-canonical-graph",
            "signals": projection_signals(projection, built_at),
        })
        with self._lock:
            if self.current_path.exists():
                os.replace(self.current_path, self.previous_path)
            _atomic_json(self.current_path, projection)
            self.snapshot = projection
            self._index()
        return {
            "status": "ok", "snapshot": body_hash,
            "nodes": len(projection["nodes"]), "edges": len(projection["edges"]),
            "validation": projection.get("validation"),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def health(self) -> dict:
        return {
            "status": "ok", "snapshot": self.snapshot.get("snapshot"),
            "built_at": self.snapshot.get("built_at"),
            "nodes": len(self.snapshot.get("nodes", [])),
            "edges": len(self.snapshot.get("edges", [])),
            "validation": self.snapshot.get("validation"),
            "limits": {
                "query": MAX_QUERY, "hops": MAX_HOPS, "k": MAX_K,
                "snapshot_bytes": MAX_SNAPSHOT_BYTES,
            },
        }

    def query(self, query: str, k: int = MAX_K) -> dict:
        # Rebuild swaps snapshot/index/adj as one logical unit. Readers take the
        # same lock so a deletion rebuild cannot mix old roots with a new index.
        with self._lock:
            return self._query_locked(query, k)

    def _query_locked(self, query: str, k: int) -> dict:
        if len(query) > MAX_QUERY:
            raise ProjectionError(f"query exceeds {MAX_QUERY} characters")
        if not query.strip():
            return {
                "status": "ok", "query": query,
                "snapshot": self.snapshot.get("snapshot"),
                "built_at": self.snapshot.get("built_at"),
                "matches": [], "nodes": [], "edges": [],
                "validation": self.snapshot.get("validation"),
                "signals": signals_for_nodes(
                    self.snapshot.get("signals", {}), set()),
            }
        k = max(1, min(int(k), MAX_K))
        words = set(_TOKEN.findall(query.lower()))
        ranked = []
        for node in self.snapshot.get("nodes", []):
            label = node["label"].lower()
            label_words = set(_TOKEN.findall(label))
            overlap = len(words & label_words)
            score = overlap * 10 + (20 if query.lower() in label else 0)
            if node["type"].lower() in words:
                score += 3
            if score:
                ranked.append((score, int(node.get("updated_at") or 0), node["id"]))
        ranked.sort(reverse=True)
        roots = [nid for _, _, nid in ranked[:k]]
        node_ids, edges = set(roots), {}
        queue = deque((nid, 0) for nid in roots)
        while queue and len(node_ids) < MAX_RESULT_NODES and len(edges) < MAX_RESULT_EDGES:
            nid, depth = queue.popleft()
            if depth >= MAX_HOPS:
                continue
            for edge, other in self.adj.get(nid, []):
                edges[edge["id"]] = edge
                if other not in node_ids and len(node_ids) < MAX_RESULT_NODES:
                    node_ids.add(other)
                    queue.append((other, depth + 1))
                if len(edges) >= MAX_RESULT_EDGES:
                    break
        ordered_ids = roots + sorted(node_ids - set(roots))
        result = {
            "status": "ok", "query": query, "snapshot": self.snapshot.get("snapshot"),
            "built_at": self.snapshot.get("built_at"),
            "matches": roots, "nodes": [self.index[n] for n in ordered_ids],
            "edges": [edges[eid] for eid in sorted(edges)],
            "validation": self.snapshot.get("validation"),
            "signals": signals_for_nodes(
                self.snapshot.get("signals", {}), node_ids),
        }
        while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
            if result["nodes"]:
                dropped = result["nodes"].pop()
                node_ids.discard(dropped["id"])
                # `matches` indexes returned roots. If the 8 KiB envelope
                # forces a large root out, retaining its ID leaves consumers
                # with a dangling reference.
                result["matches"] = [
                    node_id for node_id in result["matches"]
                    if node_id != dropped["id"]
                ]
                result["edges"] = [e for e in result["edges"] if e["src"] in node_ids and e["dst"] in node_ids]
                result["signals"] = signals_for_nodes(
                    result["signals"], node_ids)
            else:
                raise ProjectionError("bounded response cannot fit the 8 KiB envelope")
        return result


class Handler(BaseHTTPRequestHandler):
    store: ProjectionStore
    input_path: Path

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                self._json(200, self.store.health())
            elif parsed.path == "/query":
                params = parse_qs(parsed.query)
                self._json(200, self.store.query(params.get("q", [""])[0], int(params.get("k", [MAX_K])[0])))
            else:
                self._json(404, {"status": "error", "error": "not found"})
        except ProjectionError as exc:
            self._json(400, {"status": "error", "error": str(exc)})
        except Exception as exc:  # fail closed without exposing a traceback
            self._json(500, {"status": "error", "error": type(exc).__name__})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/rebuild":
            self._json(404, {"status": "error", "error": "not found"})
            return
        try:
            raw = load_input_projection(self.input_path)
            self._json(200, self.store.rebuild(raw))
        except (OSError, json.JSONDecodeError, ProjectionError) as exc:
            self._json(400, {"status": "error", "error": str(exc)[:240]})
        except Exception as exc:
            self._json(500, {"status": "error", "error": type(exc).__name__})

    def log_message(self, fmt: str, *args: object) -> None:
        print("semantica-service:", fmt % args, file=sys.stderr)


def maintain_snapshots(store: ProjectionStore, interval: float = MAINTENANCE_INTERVAL,
                       stop_event: threading.Event | None = None) -> None:
    """Prune expired derived recovery state independently of read requests."""
    stop = stop_event or threading.Event()
    while not stop.wait(interval):
        store.prune_expired_previous()


def start_snapshot_maintenance(store: ProjectionStore) -> threading.Thread:
    thread = threading.Thread(
        target=maintain_snapshots, args=(store,),
        name="semantica-snapshot-maintenance", daemon=True,
    )
    thread.start()
    return thread


def load_input_projection(path: Path) -> dict:
    """Load one bounded source file; reject oversized input before reading it."""
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ProjectionError(
            f"projection input exceeds {MAX_SNAPSHOT_BYTES}-byte ceiling")
    return json.loads(path.read_text())


def export_source(output: Path) -> dict:
    """Export the canonical Hermes graph through its own privacy allowlist."""
    orchestrator = Path(__file__).resolve().parent
    if str(orchestrator) not in sys.path:
        sys.path.insert(0, str(orchestrator))
    from dashboard import graph_memory

    projection = graph_memory.export_semantica_projection()
    _atomic_json(output, projection)
    os.chmod(output, 0o644)  # parent tree is 700; container uid needs read access
    return {"status": "ok", "nodes": len(projection["nodes"]), "edges": len(projection["edges"]), "output": str(output)}


def serve(host: str, port: int, data_dir: Path, input_path: Path) -> None:
    Handler.store = ProjectionStore(data_dir)
    Handler.input_path = input_path
    start_snapshot_maintenance(Handler.store)
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve_unix(socket_path: Path, data_dir: Path, input_path: Path) -> None:
    Handler.store = ProjectionStore(data_dir)
    Handler.input_path = input_path
    start_snapshot_maintenance(Handler.store)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = ThreadingUnixHTTPServer(str(socket_path), Handler)
    os.chmod(socket_path, 0o666)  # outer host directory is mode 700
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="verb", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("serve")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--data", type=Path, default=Path("/data"))
    run.add_argument("--input", type=Path, default=Path("/input/source.json"))
    run.add_argument("--socket", type=Path)
    args = parser.parse_args(argv)
    if args.verb == "export":
        print(json.dumps(export_source(args.output), ensure_ascii=False))
        return 0
    if args.socket:
        serve_unix(args.socket, args.data, args.input)
    else:
        serve(args.host, args.port, args.data, args.input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
