import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parents[1]
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))

from semantica_service import (  # noqa: E402
    EDGE_BATCH,
    MAINTENANCE_INTERVAL,
    MAX_RESPONSE_BYTES,
    MAX_SNAPSHOT_BYTES,
    NODE_BATCH,
    PREVIOUS_MAX_AGE,
    ProjectionError,
    ProjectionStore,
    _atomic_json,
    load_input_projection,
    maintain_snapshots,
    projection_signals,
    sanitize_projection,
    validate_projection_shacl,
)

FIXTURE = Path(__file__).parent / "fixtures" / "semantica_context_cases.jsonl"


def cases():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def combined_projection():
    values = cases()
    return {
        "nodes": [node for case in values for node in case["nodes"]],
        "edges": [edge for case in values for edge in case["edges"]],
    }


def test_export_reads_one_over_cap_and_refuses_silent_truncation(monkeypatch):
    from dashboard import graph_memory

    seen = []

    class Store:
        def all_graph(self, cap, include_archived):
            seen.append((cap, include_archived))
            return {
                "nodes": [
                    {"id": f"task:{i}", "type": "Task", "label": f"Task {i}"}
                    for i in range(cap)
                ],
                "edges": [],
            }

    with pytest.raises(ProjectionError, match="exceeds caps"):
        graph_memory.export_semantica_projection(Store())
    assert seen == [(5001, False)]


def test_fixture_has_50_independent_historical_cases():
    values = cases()
    assert len(values) == 50
    assert len({case["case_id"] for case in values}) == 50
    assert all(len(case["expected_path"]) >= 3 for case in values)
    assert {case["snapshot"] for case in values} == {"current", "historical"}
    assert {case["scenario"] for case in values} == {
        "superseded_fact", "changed_task_status", "renamed_project",
        "deleted_input", "conflicting_fact", "duplicate_ingestion", "baseline",
    }
    assert any(len(case["edges"]) != len({edge["id"] for edge in case["edges"]}) for case in values)
    assert (NODE_BATCH, EDGE_BATCH) == (200, 500)


def test_privacy_allowlist_redacts_pii_and_drops_bodies():
    raw = {
        "nodes": [{
            "id": "task:1", "type": "Task",
            "label": "Call maria@example.com at +52 81 1234 5678",
            "properties": {
                "status": "active", "ref": "/home/operator/dev/acme/plan.md",
                "text": "governed memory body", "message": "raw transcript",
                "contact_name": "Maria",
            },
        }],
        "edges": [],
    }
    clean = sanitize_projection(raw)
    encoded = json.dumps(clean)
    assert "maria@example.com" not in encoded
    assert "1234 5678" not in encoded
    assert "governed memory body" not in encoded
    assert "raw transcript" not in encoded
    assert "Maria" not in encoded
    assert "<home>/dev/acme/plan.md" in encoded
    assert clean["nodes"][0]["properties"]["status"] == "active"


def test_shacl_gate_is_real_and_rejects_known_bad_graph():
    valid = sanitize_projection({
        "nodes": [{"id": "task:shacl", "type": "Task", "label": "SHACL gate"}],
        "edges": [],
    })
    report = validate_projection_shacl(valid)
    assert report["conforms"] is True
    assert report["standard"] == "W3C SHACL"
    broken = json.loads(json.dumps(valid))
    del broken["nodes"][0]["label"]
    with pytest.raises(ProjectionError, match=r"SHACL validation failed \(1 violations\)"):
        validate_projection_shacl(broken)


def test_shacl_accepts_valid_nodes_and_multiple_distinct_edges():
    valid = sanitize_projection({
        "nodes": [
            {"id": "a", "type": "Task", "label": "A"},
            {"id": "b", "type": "Project", "label": "B"},
            {"id": "c", "type": "Decision", "label": "C"},
        ],
        "edges": [
            {"id": "e1", "src": "a", "dst": "b", "type": "PART_OF"},
            {"id": "e2", "src": "c", "dst": "b", "type": "DECIDED_IN"},
        ],
    })
    report = validate_projection_shacl(valid)
    assert report == {
        "standard": "W3C SHACL",
        "engine": "pySHACL-0.30.1 (Semantica 0.6.5 contract)",
        "conforms": True,
        "violations": 0,
    }


@pytest.mark.parametrize(("section", "field", "value"), [
    ("node", "id", None),
    ("node", "id", ""),
    ("node", "type", "UntrustedType"),
    ("node", "created_at", "not-an-integer"),
    ("node", "updated_at", "not-an-integer"),
    ("edge", "id", None),
    ("edge", "id", ""),
    ("edge", "type", "UNTRUSTED_EDGE"),
    ("edge", "src", None),
    ("edge", "dst", None),
])
def test_shacl_rejects_each_bounded_schema_violation(section, field, value):
    valid = sanitize_projection({
        "nodes": [
            {"id": "task:a", "type": "Task", "label": "A"},
            {"id": "project:b", "type": "Project", "label": "B"},
        ],
        "edges": [{"id": "edge:a-b", "src": "task:a", "dst": "project:b", "type": "PART_OF"}],
    })
    target = valid["nodes"][0] if section == "node" else valid["edges"][0]
    target[field] = value
    with pytest.raises(ProjectionError, match="SHACL validation failed"):
        validate_projection_shacl(valid)


def test_temporal_supersede_and_conflict_signals_are_bounded_and_body_free():
    as_of = 2_000_000_000
    projection = sanitize_projection({
        "nodes": [
            {"id": "decision:old", "type": "Decision", "label": "Use A",
             "updated_at": 1, "properties": {"status": "superseded", "ref": "decision:shared"}},
            {"id": "decision:new", "type": "Decision", "label": "Use B",
             "updated_at": as_of, "properties": {"status": "active", "ref": "decision:shared"}},
        ],
        "edges": [],
    })
    signals = projection_signals(projection, as_of)
    assert signals["superseded_node_ids"] == ["decision:old"]
    assert signals["stale_node_ids"] == ["decision:old"]
    assert signals["conflicts"][0]["node_ids"] == ["decision:new", "decision:old"]
    assert "decision:shared" not in json.dumps(signals)
    assert len(signals["conflicts"]) <= 10
    single = projection_signals({"nodes": [projection["nodes"][0]]}, as_of)
    assert single["conflicts"] == []
    identical = json.loads(json.dumps(projection["nodes"][0]))
    identical["id"] = "decision:old-copy"
    assert projection_signals({"nodes": [projection["nodes"][0], identical]}, as_of)["conflicts"] == []
    label_only = json.loads(json.dumps(identical))
    label_only["label"] = "Different label"
    assert len(projection_signals({"nodes": [projection["nodes"][0], label_only]}, as_of)["conflicts"]) == 1
    status_only = json.loads(json.dumps(identical))
    status_only["properties"]["status"] = "active"
    assert len(projection_signals({"nodes": [projection["nodes"][0], status_only]}, as_of)["conflicts"]) == 1

    boundary = json.loads(json.dumps(projection["nodes"][0]))
    boundary["updated_at"] = as_of - (14 * 24 * 60 * 60)
    assert projection_signals({"nodes": [boundary]}, as_of)["stale_node_ids"] == []
    boundary["updated_at"] = 0
    assert projection_signals({"nodes": [boundary]}, as_of)["stale_node_ids"] == []


def test_signal_lists_stop_at_ten():
    as_of = 2_000_000_000
    projection = sanitize_projection({
        "nodes": [
            {"id": f"decision:{i}", "type": "Decision", "label": f"Value {i}",
             "updated_at": 1, "properties": {"status": "superseded", "ref": f"ref:{i // 2}"}}
            for i in range(24)
        ],
        "edges": [],
    })
    signals = projection_signals(projection, as_of)
    assert len(signals["superseded_node_ids"]) == 10
    assert len(signals["stale_node_ids"]) == 10
    assert len(signals["conflicts"]) == 10

    one_ref = sanitize_projection({
        "nodes": [
            {"id": f"decision:shared:{i}", "type": "Decision", "label": f"Value {i}",
             "properties": {"status": "active" if i % 2 else "superseded", "ref": "shared"}}
            for i in range(12)
        ],
        "edges": [],
    })
    shared = projection_signals(one_ref, as_of)
    assert len(shared["conflicts"]) == 1
    assert len(shared["conflicts"][0]["node_ids"]) == 10


def test_snapshot_has_explicit_64_mib_ceiling(tmp_path, monkeypatch):
    import semantica_service as service

    assert MAX_SNAPSHOT_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(service, "MAX_SNAPSHOT_BYTES", 64)
    with pytest.raises(ProjectionError, match="derived snapshot exceeds"):
        _atomic_json(tmp_path / "too-large.json", {"value": "x" * 100})
    assert not (tmp_path / "too-large.json").exists()


@pytest.mark.parametrize("leak", [
    "api_key=abcdefghijklmnopqrstuvwxyz",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    "-----BEGIN PRIVATE KEY-----",
])
def test_secret_like_input_is_refused_before_persistence(leak):
    with pytest.raises(ProjectionError, match="secret-like"):
        sanitize_projection({
            "nodes": [{"id": "note:1", "type": "Note", "label": leak}],
            "edges": [],
        })


def test_rebuild_is_idempotent_deletes_removed_nodes_and_keeps_two_snapshots(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    raw = combined_projection()
    first = store.rebuild(raw)
    second = store.rebuild(raw)
    assert first["snapshot"] == second["snapshot"]
    assert second["nodes"] == 150 and second["edges"] == 100
    assert sorted(path.name for path in tmp_path.glob("*.json")) == ["current.json", "previous.json"]
    trimmed = {"nodes": raw["nodes"][:-3], "edges": raw["edges"][:-2]}
    third = store.rebuild(trimmed)
    assert third["nodes"] == 147 and third["edges"] == 98
    assert "decision:50" not in {node["id"] for node in store.query("decision 50")["nodes"]}


def test_snapshot_hash_is_canonical_across_property_key_order(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    first = {"nodes": [{"id": "task:ordered", "type": "Task", "label": "Ordered",
                        "properties": {"status": "active", "ref": "task:ordered"}}], "edges": []}
    second = {"nodes": [{"id": "task:ordered", "type": "Task", "label": "Ordered",
                         "properties": {"ref": "task:ordered", "status": "active"}}], "edges": []}
    assert store.rebuild(first)["snapshot"] == store.rebuild(second)["snapshot"]


def test_real_semantica_rebuild_attaches_prov_o_and_shacl(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=True)
    result = store.rebuild({
        "nodes": [{"id": "task:real", "type": "Task", "label": "Real path",
                   "properties": {"ref": "task:real"}}],
        "edges": [],
    })
    assert result["validation"]["conforms"] is True
    assert result["validation"]["violations"] == 0
    assert result["snapshot"] == store.health()["snapshot"]
    assert store.health()["built_at"] is not None
    assert "signals" in store.snapshot
    node = store.query("Real path")["nodes"][0]
    assert node["provenance"]["standard"] == "W3C PROV-O"
    assert node["provenance"]["engine"] == "semantica-0.6.5"


def test_persistence_query_bounds_and_response_budget(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    store.rebuild(combined_projection())
    result = store.query("decision 01", k=1)
    assert "decision:01" in result["matches"]
    assert {"decision:01", "project:01", "task:01"} <= {n["id"] for n in result["nodes"]}
    assert len(result["nodes"]) <= 50
    assert len(result["edges"]) <= 100
    assert len(json.dumps(result).encode()) <= MAX_RESPONSE_BYTES
    with pytest.raises(ProjectionError, match="512"):
        store.query("x" * 513)
    loaded = ProjectionStore(tmp_path, use_semantica=False)
    assert loaded.health()["snapshot"] == store.health()["snapshot"]
    assert loaded.query("decision 01")["matches"] == store.query("decision 01")["matches"]


def test_bounded_response_never_leaves_dangling_match_ids(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    property_keys = [
        "status", "last_verified", "store", "source", "ref", "sha",
        "kanban_id", "project_id",
    ]
    nodes = [
        {
            "id": f"task:{i}", "type": "Task",
            "label": "needle " + "é" * 110,
            "properties": {key: "V" * 160 for key in property_keys},
        }
        for i in range(10)
    ]
    edges = [
        {"id": f"edge:{i}", "src": f"task:{i}", "dst": f"task:{i + 1}",
         "type": "LINKS_TO"}
        for i in range(9)
    ]
    store.rebuild({"nodes": nodes, "edges": edges})

    result = store.query("needle", k=10)
    returned = {node["id"] for node in result["nodes"]}
    assert len(result["matches"]) < 10  # fixture crossed the 8 KiB limit
    assert set(result["matches"]) <= returned
    assert result["matches"] == [node["id"] for node in result["nodes"]]
    assert [edge["id"] for edge in result["edges"]] == [
        "edge:6", "edge:7", "edge:8",
    ]
    assert all(edge["src"] in returned and edge["dst"] in returned
               for edge in result["edges"])


def test_query_signals_refer_only_to_returned_nodes(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    old = int(time.time()) - 15 * 24 * 60 * 60
    nodes = [
        {
            "id": f"task:{i}", "type": "Task",
            "label": "needle " + "L" * 110,
            "updated_at": old,
            "properties": {
                "status": "superseded", "ref": f"ref:{i}",
                "source": "V" * 160, "store": "V" * 160,
                "last_verified": "V" * 160, "sha": "V" * 160,
                "kanban_id": "V" * 160, "project_id": "V" * 160,
            },
        }
        for i in range(10)
    ]
    store.rebuild({"nodes": nodes, "edges": []})

    result = store.query("needle", k=10)
    returned = {node["id"] for node in result["nodes"]}
    signal_ids = (
        set(result["signals"]["stale_node_ids"])
        | set(result["signals"]["superseded_node_ids"])
        | {node_id for conflict in result["signals"]["conflicts"]
           for node_id in conflict["node_ids"]}
    )
    assert signal_ids <= returned

    empty = store.query("", k=10)
    assert empty["nodes"] == []
    assert empty["signals"]["stale_node_ids"] == []
    assert empty["signals"]["superseded_node_ids"] == []
    assert empty["signals"]["conflicts"] == []
    assert len(json.dumps(empty).encode()) <= MAX_RESPONSE_BYTES


def test_two_sided_conflict_requires_and_keeps_both_returned_nodes(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    store.rebuild({
        "nodes": [
            {"id": "task:a", "type": "Task", "label": "needle alpha",
             "properties": {"ref": "same", "status": "open"}},
            {"id": "task:b", "type": "Task", "label": "needle beta",
             "properties": {"ref": "same", "status": "closed"}},
        ],
        "edges": [],
    })
    result = store.query("needle", k=2)
    assert len(result["signals"]["conflicts"]) == 1
    assert set(result["signals"]["conflicts"][0]["node_ids"]) == {
        "task:a", "task:b",
    }


def test_input_size_ceiling_is_checked_before_read(tmp_path):
    source = tmp_path / "source.json"
    with source.open("wb") as handle:
        handle.truncate(MAX_SNAPSHOT_BYTES + 1)
    with pytest.raises(ProjectionError, match="input exceeds"):
        load_input_projection(source)

    class ExactLimit:
        def stat(self):
            return type("Stat", (), {"st_size": MAX_SNAPSHOT_BYTES})()

        def read_text(self):
            return '{"schema":1,"nodes":[],"edges":[]}'

    assert load_input_projection(ExactLimit())["schema"] == 1


def test_exact_query_and_response_limits_are_allowed(tmp_path, monkeypatch):
    store = ProjectionStore(tmp_path, use_semantica=False)
    store.snapshot = {
        "schema": 1, "snapshot": "x", "built_at": 1,
        "nodes": [], "edges": [], "signals": {}, "validation": {},
    }
    store._index()
    assert store.query("x" * 512)["query"] == "x" * 512

    import semantica_service as service
    real_dumps = service.json.dumps

    def exact_envelope(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("query") == "exact-envelope":
            return "x" * MAX_RESPONSE_BYTES
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(service.json, "dumps", exact_envelope)
    assert store.query("exact-envelope")["query"] == "exact-envelope"


def test_50_case_quality_gate_improves_path_recall_without_precision_loss(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    store.rebuild(combined_projection())
    base_hits = semantic_hits = base_returned = semantic_returned = expected_total = 0
    for case in cases():
        expected = set(case["expected_ids"])
        base = {node["id"] for node in case["nodes"] if case["query"].lower() in node["label"].lower()}
        semantic = {node["id"] for node in store.query(case["query"], k=1)["nodes"]}
        base_hits += len(base & expected)
        semantic_hits += len(semantic & expected)
        base_returned += len(base)
        semantic_returned += len(semantic)
        expected_total += len(expected)
    base_recall = base_hits / expected_total
    semantic_recall = semantic_hits / expected_total
    base_precision = base_hits / base_returned
    semantic_precision = semantic_hits / semantic_returned
    relative_gain = (semantic_recall - base_recall) / base_recall
    assert relative_gain >= 0.20
    assert semantic_precision >= base_precision - 0.02


def test_accelerated_100_rebuild_and_1000_read_gate(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    raw = combined_projection()
    started = time.perf_counter()
    snapshots = {store.rebuild(raw)["snapshot"] for _ in range(100)}
    assert len(snapshots) == 1
    assert time.perf_counter() - started < 30.0
    latencies = []
    for i in range(1000):
        q = f"decision {(i % 50) + 1:02d}"
        t0 = time.perf_counter()
        result = store.query(q, k=1)
        latencies.append((time.perf_counter() - t0) * 1000)
        assert result["status"] == "ok"
    latencies.sort()
    assert latencies[int(len(latencies) * 0.95) - 1] <= 750.0


def test_corrupt_snapshot_fails_closed_to_empty_projection(tmp_path):
    (tmp_path / "current.json").write_text("{not-json")
    store = ProjectionStore(tmp_path, use_semantica=False)
    assert store.health()["snapshot"] is None
    assert store.query("anything")["nodes"] == []


def test_corrupt_current_recovers_previous_and_prunes_it_after_24h(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    expected = store.rebuild(combined_projection())["snapshot"]
    store.rebuild(combined_projection())
    store.current_path.write_text("{corrupt")
    assert ProjectionStore(tmp_path, use_semantica=False).health()["snapshot"] == expected
    old = time.time() - PREVIOUS_MAX_AGE - 1
    os.utime(store.previous_path, (old, old))
    ProjectionStore(tmp_path, use_semantica=False)
    assert not store.previous_path.exists()


def test_health_is_read_only_and_maintenance_prunes_previous_after_24h(tmp_path, monkeypatch):
    store = ProjectionStore(tmp_path, use_semantica=False)
    store.rebuild(combined_projection())
    store.rebuild(combined_projection())
    assert store.previous_path.exists()
    now = time.time()
    old = now - PREVIOUS_MAX_AGE
    os.utime(store.previous_path, (old, old))
    monkeypatch.setattr("semantica_service.time.time", lambda: now)

    before = store.previous_path.read_bytes()
    health = store.health()

    assert health["status"] == "ok"
    assert store.previous_path.read_bytes() == before

    class TwoTicks:
        calls = 0

        def wait(self, interval):
            assert interval == MAINTENANCE_INTERVAL
            self.calls += 1
            return self.calls == 2

    maintain_snapshots(store, stop_event=TwoTicks())
    assert not store.previous_path.exists()

    # A fresh recovery snapshot must survive the same maintenance pass. This
    # discriminates subtraction from an accidental addition in the age check.
    store.rebuild(combined_projection())
    store.rebuild(combined_projection())
    fresh = now - 1
    os.utime(store.previous_path, (fresh, fresh))
    store.prune_expired_previous()
    assert store.previous_path.exists()


@pytest.mark.parametrize("server_name", ["serve", "serve_unix"])
def test_both_servers_start_snapshot_maintenance(tmp_path, monkeypatch, server_name):
    import semantica_service as service

    store = object()
    started = []

    class StopServing(Exception):
        pass

    class FakeServer:
        def __init__(self, *args):
            self.args = args

        def serve_forever(self):
            raise StopServing

    monkeypatch.setattr(service, "ProjectionStore", lambda _path: store)
    monkeypatch.setattr(
        service, "start_snapshot_maintenance", lambda value: started.append(value))
    monkeypatch.setattr(service, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(service, "ThreadingUnixHTTPServer", FakeServer)
    monkeypatch.setattr(service.os, "chmod", lambda *_args: None)

    with pytest.raises(StopServing):
        if server_name == "serve":
            service.serve("127.0.0.1", 8765, tmp_path, tmp_path / "source.json")
        else:
            service.serve_unix(
                tmp_path / "run" / "api.sock", tmp_path, tmp_path / "source.json")

    assert started == [store]


def test_concurrent_readers_remain_consistent_during_rebuild(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    raw = combined_projection()
    expected = store.rebuild(raw)["snapshot"]

    def reader(_):
        seen = []
        for _ in range(50):
            value = store.query("decision 01", k=1)
            assert value["status"] == "ok"
            seen.append(value["snapshot"])
        return seen

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(reader, i) for i in range(8)]
        for _ in range(20):
            assert store.rebuild(raw)["snapshot"] == expected
        snapshots = [snapshot for future in futures for snapshot in future.result()]
    assert set(snapshots) == {expected}


def test_concurrent_readers_never_mix_indexes_during_deletion_rebuild(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    full = combined_projection()
    trimmed = {"nodes": full["nodes"][:-3], "edges": full["edges"][:-2]}
    full_hash = store.rebuild(full)["snapshot"]
    trimmed_hash = store.rebuild(trimmed)["snapshot"]
    allowed = {
        full_hash: {node["id"] for node in full["nodes"]},
        trimmed_hash: {node["id"] for node in trimmed["nodes"]},
    }

    def reader(_):
        observed = []
        for _ in range(100):
            result = store.query("decision 50", k=1)
            assert result["status"] == "ok"
            assert {node["id"] for node in result["nodes"]} <= allowed[result["snapshot"]]
            observed.append(result["snapshot"])
        return observed

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(reader, i) for i in range(8)]
        for i in range(40):
            store.rebuild(full if i % 2 else trimmed)
        observed = [snapshot for future in futures for snapshot in future.result()]
    assert set(observed) <= {full_hash, trimmed_hash}


def test_deletion_rebuild_cannot_swap_index_mid_query(tmp_path):
    store = ProjectionStore(tmp_path, use_semantica=False)
    full = combined_projection()
    trimmed = {"nodes": full["nodes"][:-3], "edges": full["edges"][:-2]}
    store.rebuild(full)
    entered, rebuilt = threading.Event(), threading.Event()

    class PausingLabel(str):
        def lower(self):
            entered.set()
            rebuilt.wait(0.25)
            return super().lower()

    store.index["decision:50"]["label"] = PausingLabel("Decision 50")

    def writer():
        assert entered.wait(1.0)
        store.rebuild(trimmed)
        rebuilt.set()

    thread = threading.Thread(target=writer)
    thread.start()
    result = store.query("decision 50", k=1)
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert result["snapshot"] != store.health()["snapshot"]
    assert "decision:50" in result["matches"]


def test_client_timeout_and_malformed_response_fail_open(monkeypatch):
    from dashboard import semantica_client as client

    monkeypatch.setattr(client, "_OPEN_UNTIL", 0.0)
    monkeypatch.setattr(client, "_FAILURES", 0)
    monkeypatch.setattr(client, "_request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    assert client.query("Hermes")["status"] == "fallback"
    monkeypatch.setattr(client, "_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("malformed")))
    assert client.query("Hermes")["status"] == "fallback"


def test_client_rejects_oversized_and_stale_projection_responses(monkeypatch):
    from dashboard import semantica_client as client

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b"x" * limit

    monkeypatch.setattr(client, "SOCKET_PATH", "/definitely/missing")
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match="8 KiB"):
        client._request("/query")

    monkeypatch.setattr(client, "_OPEN_UNTIL", 0.0)
    monkeypatch.setattr(client, "_FAILURES", 0)
    monkeypatch.setattr(client, "_request", lambda *a, **k: {
        "status": "ok", "built_at": int(time.time()) - 901,
    })
    assert client.query("Hermes") == {"status": "fallback", "reason": "stale"}


def test_client_fallback_does_not_change_canonical_recall(monkeypatch):
    from dashboard import api

    canonical = {"query": "x", "k": 8, "count": 1, "results": [{"id": "task:1"}]}
    monkeypatch.setattr(api.gmem, "recall", lambda *a, **k: json.loads(json.dumps(canonical)))
    monkeypatch.setattr(api.semantica_client, "enabled", lambda: True)
    monkeypatch.setattr(api.semantica_client, "query", lambda *a, **k: {"status": "fallback", "reason": "unavailable"})
    result = asyncio.run(api.api_recall("x"))
    assert result == canonical


def test_flag_off_is_byte_shape_identical_and_skips_projection(monkeypatch):
    from dashboard import api

    canonical = {"query": "x", "k": 8, "count": 1, "results": [{"id": "task:1"}]}
    monkeypatch.setattr(api.gmem, "recall", lambda *a, **k: json.loads(json.dumps(canonical)))
    monkeypatch.setattr(api.semantica_client, "enabled", lambda: False)
    monkeypatch.setattr(api.semantica_client, "query", lambda *a, **k: pytest.fail("projection called while disabled"))
    assert asyncio.run(api.api_recall("x")) == canonical


def test_mcp_recall_uses_same_semantica_enrichment_without_new_tool(monkeypatch):
    import mcp_server

    canonical = {"query": "x", "k": 8, "count": 1, "results": [{"id": "task:1"}]}
    semantic = {"status": "ok", "snapshot": "abc", "nodes": []}
    monkeypatch.setattr(mcp_server, "_need_loop", lambda: None)
    monkeypatch.setattr(mcp_server, "ACTIVE_SCOPE", "default")
    from dashboard import graph_memory, semantica_client
    monkeypatch.setattr(graph_memory, "recall", lambda *a, **k: json.loads(json.dumps(canonical)))
    monkeypatch.setattr(semantica_client, "enabled", lambda: True)
    monkeypatch.setattr(semantica_client, "query", lambda *a, **k: semantic)

    result = json.loads(mcp_server.tool_recall({"query": "x", "k": 8}))
    assert result == {**canonical, "semantic_context": semantic}
    assert "semantica" not in {tool["name"] for tool in mcp_server.TOOLS}


@pytest.mark.parametrize("enabled,response", [
    (False, None),
    (True, {"status": "fallback", "reason": "unavailable"}),
])
def test_mcp_recall_semantica_off_or_failed_preserves_canonical_shape(
        monkeypatch, enabled, response):
    import mcp_server
    from dashboard import graph_memory, semantica_client

    canonical = {"query": "x", "k": 8, "count": 1, "results": [{"id": "task:1"}]}
    monkeypatch.setattr(mcp_server, "_need_loop", lambda: None)
    monkeypatch.setattr(graph_memory, "recall", lambda *a, **k: json.loads(json.dumps(canonical)))
    monkeypatch.setattr(semantica_client, "enabled", lambda: enabled)
    if enabled:
        monkeypatch.setattr(semantica_client, "query", lambda *a, **k: response)
    else:
        monkeypatch.setattr(
            semantica_client, "query",
            lambda *a, **k: pytest.fail("projection called while disabled"))

    assert json.loads(mcp_server.tool_recall({"query": "x"})) == canonical
