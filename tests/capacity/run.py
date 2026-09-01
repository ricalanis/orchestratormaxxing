#!/usr/bin/env python3
"""Offline acceptance contract for bin/capacity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[2]
TOOL = Path(os.environ.get("CAPACITY_TOOL_UNDER_TEST", ROOT / "bin" / "capacity"))
FIXTURE_CODEX = Path(__file__).with_name("fake_codex.py")


def fail(message: str) -> None:
    raise AssertionError(message)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


class State:
    mode = "ok"
    requests: list[tuple[str, str]] = []


PAYLOAD = {
    "claude": {
        "available": True,
        "limits": {
            "session": {"pct": 41, "resets_at": "2026-08-12T18:00:00+00:00"},
            "weekly": {"pct": 69.9, "resets_at": "2026-08-15T04:00:00+00:00"},
            "raw": [{
                "kind": "weekly_scoped", "percent": 90,
                "resets_at": "2026-08-15T04:00:00+00:00",
                "scope": {"model": {"display_name": "Fable"}},
            }],
            "source": "live",
        },
        "credential_forbidden": "sk-test-must-never-leak",
    },
    "ollama": {
        "available": True,
        "capacity": {
            "source": "real", "tier": "max",
            "session": {"pct": 12.5, "resets_at": "2 hours."},
            "weekly": {"pct": 70, "resets_at": "4 days."},
        },
    },
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        State.requests.append(("GET", self.path))
        if State.mode == "down":
            self._send(503, {"error": "fixture down"})
            return
        payload = json.loads(json.dumps(PAYLOAD))
        if State.mode == "claude_down":
            payload["claude"] = {"available": False, "limits": {}}
        elif State.mode == "ollama_last_good":
            payload["ollama"]["capacity"]["refresh_error"] = "page still loading"
        self._send(200, payload)

    def do_POST(self):  # noqa: N802
        State.requests.append(("POST", self.path))
        if State.mode == "refresh_ollama_down" and self.path.endswith("refresh-ollama"):
            self._send(503, {"ok": False, "error": "scraper down"})
            return
        self._send(200, {"ok": True})

    def log_message(self, *_args):
        return


def run(tool: Path, base_url: str, fake_bin: Path, extra: list[str] | None = None,
        env_extra: dict[str, str] | None = None, cwd: str | None = None):
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "CAPACITY_HERMES_URL": base_url,
        "CAPACITY_TIMEOUT": "2",
    })
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(tool), *(extra or [])], text=True, capture_output=True,
        env=env, cwd=cwd, timeout=10,
    )


def main() -> int:
    check(TOOL.is_file(), f"C0 RED: missing executable {TOOL}")
    with tempfile.TemporaryDirectory(prefix="capacity-contract-") as temp:
        scratch = Path(temp)
        fake_bin = scratch / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        shutil.copy2(FIXTURE_CODEX, fake_codex)
        fake_codex.chmod(0o755)
        codex_log = scratch / "codex.jsonl"

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            # C1: help and invalid usage are typed.
            result = run(TOOL, base, fake_bin, ["--help"])
            check(result.returncode == 0 and "--refresh" in result.stdout and "--json" in result.stdout,
                  f"C1 help failed: rc={result.returncode} out={result.stdout} err={result.stderr}")
            result = run(TOOL, base, fake_bin, ["--unknown"])
            check(result.returncode == 2, f"C1 invalid usage exited {result.returncode}, expected 2")

            # C2: structured extraction, thresholds, secrets, and JSON-RPC shape.
            State.mode = "ok"
            State.requests.clear()
            result = run(TOOL, base, fake_bin, ["--json"], {
                "CAPACITY_FAKE_CODEX_LOG": str(codex_log),
            })
            check(result.returncode == 0, f"C2 json failed: {result.stderr}")
            data = json.loads(result.stdout)
            providers = data["providers"]
            check(providers["claude"]["weekly"]["used_percent"] == 69.9,
                  "C2 Claude weekly percentage changed")
            check(providers["claude"]["scoped"]["label"] == "Fable"
                  and providers["claude"]["scoped"]["used_percent"] == 90,
                  "C2 Fable weekly_scoped extraction failed")
            check(providers["claude"]["status"] == "critical", "C2 90% must be critical")
            check(providers["ollama"]["status"] == "warning", "C2 70% must be warning")
            check(providers["codex"]["status"] == "ok", "C2 69% must be ok")
            check(providers["codex"]["weekly"]["used_percent"] == 69,
                  "C2 Codex used percentage changed")
            check(providers["codex"]["credits"]["balance"] == "2500"
                  and providers["codex"]["reset_credits"] == 2,
                  "C2 Codex credits/reset-credit extraction failed")
            check("sk-test" not in result.stdout and "credential_forbidden" not in result.stdout,
                  "C2 JSON leaked an upstream credential field")
            rpc = [json.loads(line) for line in codex_log.read_text().splitlines()]
            check(rpc[0]["method"] == "initialize", "C2 Codex initialize was not first")
            rate = next(item for item in rpc if item.get("method") == "account/rateLimits/read")
            check("params" in rate and rate["params"] is None,
                  "C2 rateLimits request must carry params:null")

            # C3: compact human output has exact ten-cell bars and reset values.
            result = run(TOOL, base, fake_bin)
            check(result.returncode == 0, f"C3 text failed: {result.stderr}")
            check("███████░░░" in result.stdout, "C3 expected a ten-cell 69.9% bar")
            check("90% Fable" in result.stdout and "2500 credits" in result.stdout
                  and "2 resets" in result.stdout, "C3 compact fields missing")
            check("Claude Aug 14 22:00" in result.stdout,
                  f"C3 Claude reset not converted to Monterrey time: {result.stdout}")
            check("Ollama 4 days" in result.stdout and "Codex Aug 17" in result.stdout,
                  "C3 reset summary missing")
            check(len([line for line in result.stdout.splitlines() if line.strip()]) <= 6,
                  "C3 default output exceeded six non-empty lines")

            # C4: refresh calls both endpoints; partial refresh failure stays visible.
            State.mode = "refresh_ollama_down"
            State.requests.clear()
            result = run(TOOL, base, fake_bin, ["--refresh", "--json"])
            check(result.returncode == 0, f"C4 partial refresh should degrade, got {result.returncode}")
            refreshed = json.loads(result.stdout)
            check(("POST", "/api/usage/refresh-claude") in State.requests
                  and ("POST", "/api/usage/refresh-ollama") in State.requests
                  and ("GET", "/api/usage") in State.requests,
                  f"C4 refresh request set wrong: {State.requests}")
            check("ollama" in refreshed["refresh_errors"], "C4 refresh failure was hidden")

            State.mode = "ollama_last_good"
            result = run(TOOL, base, fake_bin, ["--json"])
            last_good = json.loads(result.stdout)["providers"]["ollama"]
            check(last_good["available"] and last_good["status"] == "warning"
                  and last_good["refresh_error"] == "page still loading",
                  "C4 last-good reading or its refresh warning was hidden")

            # C5: one provider can fail; all-provider failure is exit 1.
            State.mode = "claude_down"
            result = run(TOOL, base, fake_bin)
            check(result.returncode == 0 and "Claude" in result.stdout
                  and "unavailable" in result.stdout,
                  "C5 partial provider failure did not degrade visibly")
            State.mode = "down"
            result = run(TOOL, base, fake_bin, ["--json"], {
                "CAPACITY_CODEX_BIN": str(scratch / "missing-codex"),
            })
            check(result.returncode == 1, f"C5 all-provider failure exited {result.returncode}")
            failed = json.loads(result.stdout)
            check(all(not item["available"] for item in failed["providers"].values()),
                  "C5 all-provider JSON did not mark every source unavailable")

            # C6: malformed Codex never poisons healthy Hermes providers.
            State.mode = "ok"
            result = run(TOOL, base, fake_bin, ["--json"], {
                "CAPACITY_FAKE_CODEX_MODE": "malformed",
            })
            malformed = json.loads(result.stdout)
            check(result.returncode == 0 and not malformed["providers"]["codex"]["available"]
                  and malformed["providers"]["claude"]["available"],
                  "C6 malformed Codex did not isolate to Codex")

            # C7: the installed-style copy runs from / with no repository imports.
            deployed = scratch / "deployed-capacity"
            shutil.copy2(TOOL, deployed)
            deployed.chmod(0o755)
            result = run(deployed, base, fake_bin, ["--json"], cwd="/")
            check(result.returncode == 0 and json.loads(result.stdout)["providers"]["ollama"]["available"],
                  f"C7 installed-style copy failed: {result.stderr}")

            # capacity is a fleet tool: it ships from the private install-fleet.sh (the graduated
            # core install.sh no longer copies fleet bridges), so read both installers.
            install_text = (ROOT / "install.sh").read_text()
            fleet_installer = ROOT / "install-fleet.sh"
            if fleet_installer.is_file():
                install_text += "\n" + fleet_installer.read_text()
            check('bin/capacity"' in install_text and '"$BIN_DST/capacity"' in install_text,
                  "C7 install.sh does not copy/chmod capacity")

            # C8: a remote fleet member re-execs the command on the Ubuntu host.
            ssh_log = scratch / "ssh-argv.txt"
            ssh_identity = scratch / "id_ed25519"
            ssh_identity.write_text("fixture identity\n")
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/bin/sh\n"
                ": > \"$CAPACITY_FAKE_SSH_LOG\"\n"
                "for arg in \"$@\"; do printf '%s\\n' \"$arg\" >> \"$CAPACITY_FAKE_SSH_LOG\"; done\n"
                "printf '%s\\n' \"$CAPACITY_FAKE_REMOTE_JSON\"\n"
            )
            fake_ssh.chmod(0o755)
            remote_env = os.environ.copy()
            remote_env.update({
                "PATH": f"{fake_bin}:{remote_env.get('PATH', '')}",
                "CAPACITY_REMOTE": "always",
                "CAPACITY_SSH_TARGET": "fixture@ubuntu.test",
                "CAPACITY_SSH_IDENTITY": str(ssh_identity),
                "CAPACITY_FAKE_SSH_LOG": str(ssh_log),
                "CAPACITY_FAKE_REMOTE_JSON": json.dumps({
                    "usage": PAYLOAD, "refresh_errors": {}, "shared_error": None,
                }),
            })
            remote_env.pop("CAPACITY_HERMES_URL", None)
            result = subprocess.run(
                [str(TOOL), "--json", "--refresh", "--timeout", "3"],
                text=True, capture_output=True, env=remote_env, timeout=10,
            )
            remote_report = json.loads(result.stdout)
            check(result.returncode == 0
                  and isinstance(remote_report.get("providers"), dict)
                  and all(item["available"] for item in remote_report["providers"].values()),
                  f"C8 remote fleet route failed: rc={result.returncode} "
                  f"out={result.stdout} err={result.stderr}")
            ssh_args = ssh_log.read_text().splitlines()
            check("-T" in ssh_args and "BatchMode=yes" in ssh_args
                  and "IdentitiesOnly=yes" in ssh_args,
                  f"C8 remote ssh safety flags missing: {ssh_args}")
            check("fixture@ubuntu.test" in ssh_args,
                  f"C8 configured ssh target missing: {ssh_args}")
            remote_command = ssh_args[-1]
            check("--local" in remote_command and "--hermes-json" in remote_command
                  and "--refresh" in remote_command and "--timeout" in remote_command,
                  f"C8 remote command lost flags or recursion guard: {remote_command}")

            # C9: the remote re-entry guard must force the local HTTP path.
            ssh_log.unlink()
            result = run(TOOL, base, fake_bin, ["--local", "--json"], {
                "CAPACITY_REMOTE": "always",
                "CAPACITY_FAKE_SSH_LOG": str(ssh_log),
            })
            check(result.returncode == 0
                  and json.loads(result.stdout)["providers"]["claude"]["available"],
                  f"C9 --local did not use the local Hermes path: {result.stderr}")
            check(not ssh_log.exists(), "C9 --local still invoked ssh — recursion risk")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("capacity contract: 9/9 PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"capacity contract: {exc}", file=sys.stderr)
        raise SystemExit(1)
