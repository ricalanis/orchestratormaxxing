#!/usr/bin/env python3
"""Check declared projection accounting; never execute or certify its evidence."""
import argparse
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys

LIMIT = 262144


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("projection documents must be regular files")
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("projection document exceeds size limit")
    return json.loads(raw, object_pairs_hook=unique_object), hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return isinstance(value, list) and all(text(x) for x in value) and len(set(value)) == len(value)


def fields(obj, expected):
    require(isinstance(obj, dict) and set(obj) == set(expected), "unexpected or missing fields")


def evidence(obj):
    fields(obj, ("status", "evidence"))
    require(obj["status"] == "pass" and text(obj["evidence"]), "required evidence is not passing")


def public_path(value):
    if not text(value) or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value and value != "."


def verify(contract, report, contract_hash):
    fields(contract, ("schema_version", "base", "head", "capabilities"))
    fields(report, ("schema_version", "base", "head", "contract_sha256", "capabilities"))
    for obj in (contract, report):
        require(type(obj["schema_version"]) is int and obj["schema_version"] == 1, "unsupported schema")
        for key in ("base", "head"):
            require(isinstance(obj[key], str) and re.fullmatch(r"[0-9a-f]{40}", obj[key]), "invalid revision")
        require(isinstance(obj["capabilities"], list) and obj["capabilities"], "capability scope is empty")
    require(report["base"] == contract["base"] and report["head"] == contract["head"], "revision mismatch")
    require(report["contract_sha256"] == contract_hash, "contract hash mismatch")
    expected = {}
    for item in contract["capabilities"]:
        fields(item, ("id", "required", "hosts"))
        require(text(item["id"]) and item["id"] not in expected, "invalid or duplicate capability id")
        require(type(item["required"]) is bool, "required must be boolean")
        require(strings(item["hosts"]), "invalid host scope")
        expected[item["id"]] = item
    seen = set()
    counts = dict(declared=len(expected), included=0, deferred=0, excluded=0)
    for item in report["capabilities"]:
        require(isinstance(item, dict), "capability must be an object")
        name = item.get("id")
        require(text(name) and name in expected and name not in seen, "unknown or duplicate capability id")
        seen.add(name)
        disposition = item.get("disposition")
        require(isinstance(disposition, str) and disposition in {"included", "deferred", "excluded"}, "invalid disposition")
        common = ("id", "disposition", "source_evidence", "reason")
        require(text(item.get("source_evidence")) and text(item.get("reason")), "missing projection explanation")
        if disposition != "included":
            fields(item, common)
            require(not expected[name]["required"], "required capability was omitted")
        else:
            fields(item, (*common, "public_behavior", "public_paths", "dependencies", "functional", "security", "hosts"))
            require(text(item["public_behavior"]), "missing public behavior")
            require(strings(item["public_paths"]) and item["public_paths"] and all(public_path(x) for x in item["public_paths"]), "invalid public paths")
            for kind in ("dependencies", "functional", "security"):
                evidence(item[kind])
            require(isinstance(item["hosts"], dict) and set(item["hosts"]) == set(expected[name]["hosts"]), "host scope mismatch")
            for proof in item["hosts"].values():
                evidence(proof)
        counts[disposition] += 1
    require(seen == set(expected), "declared capability rows are missing")
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        contract, digest = read_document(args.contract)
        report, _ = read_document(args.report)
        counts = verify(contract, report, digest)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"structurally_complete": False, "error": str(exc)}))
        return 1
    print(json.dumps({"structurally_complete": True, "coverage": counts,
                      "evidence_verified": False, "publication_authorized": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
