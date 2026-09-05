#!/usr/bin/env python3
"""Portable planner and installed-host contracts; no live model or service calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "plugins/orchestratormaxxing/skills"
PLAN = "SUMMARY\nA useful plan.\nSTEPS\n1. Read the target.\nCONTRACT\nRun the check.\nEXECUTION SHAPE\nROOT-DIRECT\nRISKS / ASSUMPTIONS\nNone.\nOUT OF SCOPE\nImplementation.\n"
PORTABLE = {"astraplan", "solplan", "cheap-delegate", "fanout", "omaxxing-public-improve", "public-improve-security"}


class AstraContract(unittest.TestCase):
    def test_portable_pair_crosses_real_child_boundary_with_explicit_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name in ("solplan", "astraplan"):
                shutil.copytree(SKILLS / name, path / name, ignore=shutil.ignore_patterns("__pycache__"))
            fake = path / "codex"
            fake.write_text("#!" + sys.executable + "\nimport sys,os,json\nfrom pathlib import Path\na=sys.argv[1:]\n"
                            "assert a[a.index('--model')+1]=='gpt-6-astra'\n"
                            "assert a[a.index('--sandbox')+1]=='read-only'\n"
                            "assert 'model_reasoning_effort=\"ultra\"' in a\n"
                            "assert '--ignore-user-config' in a and '--ephemeral' in a\n"
                            "assert 'agents.max_threads=4' in a and 'agents.max_depth=1' in a\n"
                            "assert os.environ.get('ORCHESTRATORMAXXING_HARNESS_CHILD')=='1'\n"
                            "assert sys.stdin.read()==''\n"
                            "Path(a[a.index('--output-last-message')+1]).write_text(" + repr(PLAN) + ")\n"
                            "print(json.dumps({'type':'turn.completed'}))\n")
            fake.chmod(0o755)
            result = subprocess.run([sys.executable, str(path / "astraplan/scripts/run_astraplan.py"),
                                     "--workdir", tmp, "--codex-bin", str(fake)], input="Bounded brief",
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), PLAN.strip())
            self.assertIn("Astra", result.stderr)
            shutil.rmtree(path / "solplan")
            result = subprocess.run([sys.executable, str(path / "astraplan/scripts/run_astraplan.py")],
                                    input="Brief", capture_output=True, text=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("paired solplan engine missing", result.stderr)

    def test_explicit_sol_and_unknown_profile(self):
        spec = importlib.util.spec_from_file_location("engine", SKILLS / "solplan/scripts/run_solplan.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for profile, model in [("sol", "gpt-5.6-sol"), ("astra", "gpt-6-astra")]:
            argv = module.command(codex="codex", output=Path("result"), workdir=Path("."), brief="x", planner=profile)
            self.assertEqual(argv[argv.index("--model") + 1], model)
        with self.assertRaises(ValueError):
            module.command(codex="codex", output=Path("result"), workdir=Path("."), brief="x", planner="unknown")

    def test_real_skill_sync_installs_complete_portable_dependencies(self):
        data = json.loads((ROOT / "skills/external-stack.json").read_text())
        data["skills"] = [item for item in data["skills"] if item["name"] in PORTABLE]
        self.assertEqual({x["name"] for x in data["skills"]}, PORTABLE)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); (home / ".config/zed").mkdir(parents=True, exist_ok=True)
            (home / ".hermes").mkdir(); (home / ".hermes/kanban.db").touch()
            manifest = home / "manifest.json"; manifest.write_text(json.dumps(data))
            env = dict(os.environ, HOME=tmp, CODEX_HOME=str(home / ".codex"))
            args = [sys.executable, str(ROOT / "bin/sync-agent-skills"), "--manifest", str(manifest), "--offline"]
            for _ in range(2):
                result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
            for relative in [".claude/skills", ".config/opencode/skills", ".hermes/skills"]:
                for item in data["skills"]:
                    target = home / relative / item["name"]
                    self.assertTrue((target / "SKILL.md").is_file())
                    for include in item["include"]:
                        source = ROOT / item["path"] / include
                        for file in ([source] if source.is_file() else source.rglob("*")):
                            if file.is_file():
                                self.assertEqual(file.read_bytes(), (target / file.relative_to(ROOT / item["path"])).read_bytes())
            self.assertIn('model = "gpt-6-astra"', (ROOT / ".codex/agents/astra-planner.toml").read_text())

    def test_real_installer_hermes_doctrine_is_optional_preserving_and_idempotent(self):
        source = (ROOT / "install.sh").read_text()
        start = source.index('say "Orchestrator pointer')
        stop = source.index('\nsay ', start + 1)
        block = source[start:stop]
        setup = '''say() { :; }
CLAUDE_DIR="$HOME/.claude"
CODEX_DIR="$HOME/.codex"
OPENCODE_CFG_DIR="$HOME/.config/opencode"
CFG_DIR="$HOME/.config/orchestratormaxxing"
mkdir -p "$CLAUDE_DIR" "$CODEX_DIR" "$CFG_DIR"
'''
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); env = dict(os.environ, HOME=tmp, REPO_DIR=str(ROOT))
            result = subprocess.run(["bash", "-c", setup + block], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((home / ".hermes").exists())
            (home / ".config/zed").mkdir(parents=True, exist_ok=True)
            (home / ".hermes").mkdir(); (home / ".hermes/kanban.db").touch()
            target = home / ".hermes/AGENTS.md"; target.write_text("Keep my custom instructions.\n")
            for _ in range(2):
                result = subprocess.run(["bash", "-c", setup + block], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            for interactive in [home / ".config/zed/AGENTS.md", home / ".config/orchestratormaxxing/warp-global-rules.md"]:
                guidance = interactive.read_text()
                self.assertIn(str(ROOT / "plugins/orchestratormaxxing/skills/astraplan/SKILL.md"), guidance)
                self.assertIn("repository CLI entry point", guidance)
                self.assertIn("not a Zed/Warp native skill installation", guidance)
                self.assertEqual(guidance.count("<!-- orchestratormaxxing:orchestrator:begin -->"), 1)
                self.assertTrue((ROOT / "plugins/orchestratormaxxing/skills/astraplan/scripts/run_astraplan.py").is_file())
                self.assertTrue((ROOT / "plugins/orchestratormaxxing/skills/solplan/scripts/run_solplan.py").is_file())
            text = target.read_text()
            self.assertEqual(text.count("<!-- orchestratormaxxing:orchestrator:begin -->"), 1)
            for token in ["Keep my custom instructions.", "astraplan", "cheap-delegate", "o delegate/send/handoff"]:
                self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
