"""Root contract: actual subprocess argv and retained planner isolation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / 'plugins/orchestratormaxxing/skills/solplan/scripts/run_solplan.py'
ASTRA = ROOT / 'plugins/orchestratormaxxing/skills/astraplan/scripts/run_astraplan.py'
PLAN = '\n'.join(['SUMMARY', 'Plan.', 'STEPS', '1. Implement.', 'CONTRACT',
                  'Checks pass.', 'EXECUTION SHAPE', 'ROOT-DIRECT',
                  'RISKS / ASSUMPTIONS', 'None.', 'OUT OF SCOPE', 'Publication.'])


class AstraContract(unittest.TestCase):
    def test_actual_spawn_selects_model_and_retains_isolation(self):
        for runner, extra, model in [(SHARED, [], 'gpt-5.6-sol'),
                                     (SHARED, ['--model', 'gpt-6-astra'], 'gpt-6-astra'),
                                     (ASTRA, [], 'gpt-6-astra')]:
            with self.subTest(model=model, runner=runner), tempfile.TemporaryDirectory() as td:
                p = Path(td)
                fake = p / 'codex'
                fake.write_text('#!/usr/bin/env python3\nimport os,sys,json,pathlib\n'
                                'a=sys.argv[1:]\n'
                                'pathlib.Path("seen.json").write_text(json.dumps({"args":a,"child":os.getenv("ORCHESTRATORMAXXING_HARNESS_CHILD")}))\n'
                                'pathlib.Path(a[a.index("--output-last-message")+1]).write_text(' + repr(PLAN) + ')\n')
                fake.chmod(0o755)
                r = subprocess.run([sys.executable, str(runner), '--workdir', td,
                                    '--codex-bin', str(fake)] + extra,
                                   input='Plan a bounded change.', text=True, capture_output=True,
                                   cwd=td, timeout=15)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), PLAN)
                seen = json.loads((p / 'seen.json').read_text())
                args = seen['args']
                self.assertEqual(args[args.index('--model') + 1], model)
                self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
                self.assertIn('--ignore-user-config', args)
                self.assertIn('--ephemeral', args)
                self.assertIn('agents.max_depth=1', args)
                self.assertIn('agents.max_threads=4', args)
                self.assertEqual(seen['child'], '1')
                self.assertIn(model, r.stderr)

    def test_invalid_model_and_astra_demotion_refuse_before_spawn(self):
        for runner, model in [(SHARED, 'invalid-model'), (ASTRA, 'gpt-5.6-sol')]:
            with tempfile.TemporaryDirectory() as td:
                r = subprocess.run([sys.executable, str(runner), '--workdir', td,
                                    '--codex-bin', '/nonexistent/codex', '--model', model],
                                   input='Plan.', text=True, capture_output=True, timeout=10)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn('No such file', r.stderr)


if __name__ == '__main__':
    unittest.main()
