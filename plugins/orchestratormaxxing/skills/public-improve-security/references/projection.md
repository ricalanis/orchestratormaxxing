# Projection accounting schema

Root owns the contract, writes it before implementation/review and keeps it read-only.
The report accounts for every declared capability, including deliberate omissions. Both
files remain private. Schema version1 uses these exact fields; unknown fields, duplicate
IDs/JSON keys, empty evidence and missing rows fail. Files must be regular, at most256KiB.
The checker reads only these files: it does not fetch sources or execute cited commands.

Contract example (replace revisions with the actual public base/candidate):

```json
{"schema_version":1,"base":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","head":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","capabilities":[{"id":"planner-entry","required":true,"hosts":["macos","linux"]},{"id":"server-ui","required":false,"hosts":[]}]}
```

Report example (`contract_sha256` is SHA-256 of the exact contract file bytes):

```json
{
  "schema_version": 1,
  "base": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "contract_sha256": "REPLACE_WITH_CONTRACT_SHA256",
  "capabilities": [
    {
      "id": "planner-entry", "disposition": "included",
      "source_evidence": "Reviewed source interface and contract reference",
      "reason": "Preserve explicit planning through the portable runner",
      "public_behavior": "An installed entry point resolves the paired engine",
      "public_paths": ["plugins/example/skills/planner/SKILL.md"],
      "dependencies": {"status":"pass","evidence":"Paired payload inventory and installed-home check"},
      "functional": {"status":"pass","evidence":"Regression command, exit and candidate revision"},
      "security": {"status":"pass","evidence":"Resolved boundary review and scanner evidence"},
      "hosts": {
        "macos": {"status":"pass","evidence":"macOS contract receipt"},
        "linux": {"status":"pass","evidence":"Linux contract receipt"}
      }
    },
    {"id":"server-ui","disposition":"deferred","source_evidence":"Separate server adapter inventory","reason":"No server UI support is claimed in this contribution"}
  ]
}
```

Required capabilities must be included; included rows need dependency, functional,
security and every declared host evidence entry with status `pass`. Optional capabilities
may be deferred or excluded with a reason. A host that is not applicable is omitted
from that capability's root-authored `hosts` list, with the rationale recorded during
scope review; a worker cannot waive a required host in its report.

After implementation, root updates the candidate revision in the contract before the
final review, hashes it again, and records this scope revision. Never silently shrink
required scope to make a report pass. Bind both files' hashes to the final security
receipt. Structural success is **not** security approval: evidence references can be
false or stale, so root must inspect their actual results against the final Git tree.
