"""Exercise real skill payload installation without providers, SSH, or a fleet."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
NAMES = {"review-triage", "sun-earth-ssh"}


def main():
    manifest = json.loads((ROOT / "skills/external-stack.json").read_text())
    entries = [entry for entry in manifest["skills"] if entry["name"] in NAMES]
    assert {entry["name"] for entry in entries} == NAMES, "selected skills missing"
    with tempfile.TemporaryDirectory(prefix="portable-skills-") as temporary:
        work = Path(temporary)
        subset = work / "manifest.json"
        subset.write_text(json.dumps({"schema_version": 1, "skills": entries}))
        blocked = work / "blocked"
        blocked.mkdir()
        for name in ("ssh", "curl", "wget", "git", "oll", "provider-ask", "cross-review"):
            tool = blocked / name
            tool.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$PROBE_LOG"\nexit 99\n')
            tool.chmod(0o755)
        for live_hermes in (False, True):
            home = work / ("server" if live_hermes else "standalone")
            home.mkdir()
            if live_hermes:
                (home / ".hermes").mkdir()
                (home / ".hermes/kanban.db").touch()
            sentinel = home / "unrelated.txt"
            sentinel.write_text("preserve me")
            env = dict(os.environ)
            for key in ("CODEX_HOME", "ORCHESTRATORMAXXING_SKILL_CACHE"):
                env.pop(key, None)
            env.update(HOME=str(home), PATH=str(blocked) + os.pathsep + env["PATH"],
                       PROBE_LOG=str(work / "unexpected-network"))
            command = [sys.executable, str(ROOT / "bin/sync-agent-skills"),
                       "--manifest", str(subset), "--offline", "--cache-dir", str(work / "cache"),
                       "--source", "local://orchestratormaxxing=" + str(ROOT)]
            for _ in range(2):
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                assert result.returncode == 0, result.stderr
            locations = {"Claude": ".claude/skills", "Codex": ".codex/skills",
                         "OpenCode": ".config/opencode/skills", "Hermes": ".hermes/skills"}
            for entry in entries:
                source = ROOT / entry["path"]
                for host, relative in locations.items():
                    destination = home / relative / entry["name"]
                    expected = host in entry.get("targets", locations) and (host != "Hermes" or live_hermes)
                    assert destination.exists() == expected, (host, entry["name"])
                    if expected:
                        for path in source.rglob("*"):
                            if path.is_file():
                                assert (destination / path.relative_to(source)).read_bytes() == path.read_bytes()
                        marker = json.loads((destination / ".orchestratormaxxing-source.json").read_text())
                        assert marker["tree_sha256"] == entry["tree_sha256"]
            assert sentinel.read_text() == "preserve me"
            assert not (home / ".ssh").exists(), "skill installation configured SSH"
            assert not (work / "unexpected-network").exists(), "installation invoked a provider or network tool"
            # Existing user-authored skills must survive a managed install attempt.
            unmanaged = home / ".claude/skills/review-triage"
            (unmanaged / ".orchestratormaxxing-source.json").unlink()
            (unmanaged / "SKILL.md").write_text("user-owned skill")
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            assert result.returncode != 0, "unmanaged skill overwritten"
            assert (unmanaged / "SKILL.md").read_text() == "user-owned skill"
        plugin = ROOT / "plugins/orchestratormaxxing"
        metadata = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        assert (plugin / metadata["skills"] / "sun-earth-ssh/SKILL.md").is_file()
    print("portable-skills: offline native installs, plugin discovery, idempotence and user content protection passed")


if __name__ == "__main__":
    main()
