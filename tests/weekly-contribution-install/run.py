"""Execute the installer and offline skill distribution in disposable homes."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
NAMES = {"research-prompt", "plan-to-repo", "product-manager",
         "weekly-public-contribution", "agent-guard"}


def execute(args, env, cwd, timeout=90):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True,
                            text=True, timeout=timeout)
    assert result.returncode == 0, (args, result.returncode,
                                    result.stdout[-3000:], result.stderr[-3000:])
    return result.stdout


def main():
    manifest = json.loads((ROOT / "skills/external-stack.json").read_text())
    entries = [entry for entry in manifest["skills"] if entry["name"] in NAMES]
    assert {entry["name"] for entry in entries} == NAMES
    version = json.loads((ROOT / "plugins/orchestratormaxxing/.codex-plugin/plugin.json").read_text())["version"]
    with tempfile.TemporaryDirectory(prefix="weekly-install-") as temporary:
        work = Path(temporary)
        fake = work / "fake"
        fake.mkdir()
        # Only local configuration APIs are simulated; any provider/network or
        # service activation attempt is attributable and fails the contract.
        stub = fake / "stub.py"
        stub.write_text('''import json, os, sys
from pathlib import Path
name=Path(sys.argv[0]).name; args=sys.argv[1:]
if name=='codex' and args[:3]==['plugin','marketplace','list']:
 print(json.dumps({'marketplaces':[{'root':os.environ['TEST_ROOT']}]}))
elif name=='codex' and args[:2]==['plugin','add']:
 print('{}')
elif name=='codex' and args[:2]==['plugin','list']:
 print(json.dumps({'installed':[{'pluginId':'orchestratormaxxing@personal','installed':True,'enabled':True,'version':os.environ['TEST_VERSION']}]}))
elif name=='hermes' and args[:2]==['plugins','enable']:
 p=Path(os.environ['HOME'])/'.hermes/config.yaml'
 p.write_text('plugins:\\n  enabled: [command-guard, security-guidance]\\n')
elif name=='systemctl' and args==['--user','daemon-reload']:
 pass
elif name in ('launchctl','systemctl') and any(a in args for a in ('print','list','is-enabled')):
 sys.exit(1)
else:
 with open(os.environ['PROBE_LOG'],'a') as f: f.write(name+' '+repr(args)+'\\n')
 sys.exit(99)
''')
        for name in ("codex", "hermes", "launchctl", "systemctl", "ssh", "curl",
                     "wget", "oll", "provider-ask", "opencode", "git", "crontab"):
            target = fake / name
            target.write_text("#!" + sys.executable + "\n" + stub.read_text())
            target.chmod(0o755)
        subset = work / "skills.json"
        subset.write_text(json.dumps({"schema_version": 1, "skills": entries}))
        for optional_hosts in (False, True):
            home = work / ("hosts" if optional_hosts else "standalone")
            home.mkdir()
            for relative in (".claude", ".config/opencode", ".codex"):
                (home / relative).mkdir(parents=True)
            (home / ".claude/settings.json").write_text(json.dumps({"userSetting": "preserve"}))
            (home / ".config/opencode/opencode.json").write_text("{}")
            (home / ".codex/config.toml").write_text('model = "user-model"\n')
            sentinel = home / "unrelated.txt"
            sentinel.write_text("preserve")
            if optional_hosts:
                (home / ".hermes").mkdir()
                (home / ".hermes/kanban.db").touch()
                (home / ".config/zed").mkdir()
                (home / ".config/zed/settings.json").write_text('{"userSetting":"preserve"}')
                warp = home / (".warp" if sys.platform == "darwin" else ".config/warp-terminal")
                warp.mkdir(parents=True)
                (warp / "settings.toml").write_text('[agents.execution_profiles.default]\ncommand_denylist = [\n  "user-kept",\n]\n')
            env = {"HOME": str(home), "PATH": str(fake) + os.pathsep + os.environ["PATH"],
                   "USER": "fixture", "LANG": "C.UTF-8", "TEST_ROOT": str(ROOT),
                   "TEST_VERSION": version, "PROBE_LOG": str(work / "unexpected"),
                   "ORCHESTRATORMAXXING_SKIP_EXTERNAL_SKILLS": "1",
                   "ORCHESTRATORMAXXING_HARNESS_CHILD": "1", "CLAUDEMAXXING_HARNESS_CHILD": "1"}
            for _ in range(2):
                execute(["bash", str(ROOT / "install.sh")], env, work)
                execute([sys.executable, str(ROOT / "bin/sync-agent-skills"),
                         "--manifest", str(subset), "--offline", "--cache-dir", str(work / "cache"),
                         "--source", "local://orchestratormaxxing=" + str(ROOT)], env, work)
            assert sentinel.read_text() == "preserve"
            assert json.loads((home / ".claude/settings.json").read_text())["userSetting"] == "preserve"
            assert 'model = "user-model"' in (home / ".codex/config.toml").read_text()
            assert "trusted_hash" not in (home / ".codex/config.toml").read_text(), "installer silently trusted hooks"
            assert not (work / "unexpected").exists(), (work / "unexpected").read_text() if (work / "unexpected").exists() else ""
            for entry in entries:
                source = ROOT / entry["path"]
                assert source.is_dir() and (source / "SKILL.md").is_file()
                for host, relative in (("Claude", ".claude/skills"), ("OpenCode", ".config/opencode/skills"), ("Hermes", ".hermes/skills")):
                    destination = home / relative / entry["name"]
                    expected = host != "Hermes" or optional_hosts
                    assert destination.exists() == expected, (host, entry["name"])
                    if expected:
                        for path in source.rglob("*"):
                            if path.is_file():
                                assert (destination / path.relative_to(source)).read_bytes() == path.read_bytes()
            installed = home / ".local/bin"
            for name in ("agent-guard", "worker-path-bench"):
                assert (installed / name).read_bytes() == (ROOT / "bin" / name).read_bytes()
            data = home / ".config/orchestratormaxxing/agent-guard"
            for path in (ROOT / "deploy/agent-guard").iterdir():
                if path.is_file():
                    assert (data / path.name).read_bytes() == path.read_bytes()
            execute([str(installed / "agent-guard"), "selftest"], env, work)
            execute([str(installed / "agent-guard"), "status", "--gate", "--repo", str(ROOT)], env, work)
            # Use the installed tool/scorer from an unrelated cwd, not the checkout.
            execute([str(installed / "worker-path-bench"), "--cases", str(ROOT / "experiments/worker-path-bench/cases.jsonl"),
                     "--adapter", "fixture=" + shlex.join([sys.executable, str(ROOT / "experiments/worker-path-bench/fixtures/pass_adapter.py")]),
                     "--output", str(work / "raw.jsonl"), "--summary", str(work / "summary.json")], env, work)
            assert json.loads((work / "summary.json").read_text())["adapters"]["fixture"]["passed"] == 2
            if optional_hosts:
                assert json.loads((home / ".config/zed/settings.json").read_text())["userSetting"] == "preserve"
                assert "user-kept" in (warp / "settings.toml").read_text()
                assert (home / ".hermes/plugins/command-guard/__init__.py").is_file()
    print("weekly-contribution-install: full installer twice, offline skill bytes, settings preservation, guard and installed benchmark passed")


if __name__ == "__main__":
    main()
