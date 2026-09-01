import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "reflection-evening.sh"


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=False,
    )


def test_harvard_test_mode_is_complete_and_secular():
    result = run_script("--test", env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
    assert result.returncode == 0
    assert result.stderr == ""
    assert "What went well?" in result.stdout
    assert "What didn't go as planned?" in result.stdout
    assert "What will I do differently?" in result.stdout
    assert "facts → meaning → next step" in result.stdout
    assert not {"jesuit", "examen", "gratitude"} & set(result.stdout.lower().split())


def test_test_mode_is_deterministic_and_needs_no_live_dependencies():
    env = {"PATH": "/nonexistent", "HOME": "/nonexistent"}
    first = run_script("--test", env=env)
    second = run_script("--test", env=env)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_normal_mode_is_silent_without_dashboard_token(tmp_path: Path):
    result = run_script(env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_unknown_argument_fails_without_output():
    result = run_script("--unknown", env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
    assert result.returncode == 2
    assert result.stdout == result.stderr == ""


def test_script_is_executable_and_bash_syntax_is_valid():
    assert os.access(SCRIPT, os.X_OK)
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr
