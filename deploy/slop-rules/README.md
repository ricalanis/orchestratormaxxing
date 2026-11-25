# slop-rules

Python slop rules for `bin/slop-check`. The eight `*.yaml` files (197 ast-grep rules) and `LICENSE`
are copied unchanged from [gabeorlanski/scb-check](https://github.com/gabeorlanski/scb-check) commit
`a8618228939def726c2ec48b354693e5aa1999d5` (scb-check 0.2.0, the SlopCodeBench rule engine), Apache-2.0.
`default-rules.txt`, `requirements.lock` and this file are local.

- `default-rules.txt`: the 25 rules `slop-check` runs by default: the ones that fired most often on
  this harness's own Python when the tool was added. Edit the list to fit your codebase;
  `--all-rules` runs all 197.
- `requirements.lock`: hash-pinned `ast-grep-cli` 0.42.1 (Linux x86_64/aarch64 and macOS wheels).

`slop-check` is a measurement, not a worker gate. In one run, feeding these findings back once to a
cheap coding model removed 14% of the findings it was shown (671 to 578) for 76% more tokens, and the
final code had no less slop than the control arm (`knowledge/no-slop-2026-11-24.md`).

## Scope and limits

`slop-check` is a lint for repositories you trust. It is not a security boundary: do not use it to vet
code from a source you do not trust, and do not expect it to contain what such a repository does.
Diff mode avoids the git features that run repository-configured programs and the scanner runs with
its own config; that reduces what a scanned repository can influence, and it is not a guarantee.

- "Clean" means no unsuppressed finding: code can silence one with an `# ast-grep-ignore` comment.
- A Python file is a `.py` file, or any other file whose first line is a Python shebang. A file
  such as `x.pyi` without a shebang is not scanned.
- Symlinked files are not scanned and nothing reports the skip; an explicit path whose last component
  is a symlink exits 2, while a symlinked parent directory is followed. Diff mode does not scan
  submodules (it prints a note) or nested repositories; a directory scan walks into them.
- A directory scan prunes `.venv`, `.git`, `node_modules`, `__pycache__` and any directory holding a
  `pyvenv.cfg`, below the scan root.
- `--install` builds beside the old venv and swaps it in, and replaces only a directory that carries
  the tool's stamp file. `uv` runs without its config files and `pip` in isolated mode. When `uv` is
  on PATH and fails, the install exits 2; take `uv` off PATH to build with `pip`.
- git calls time out after 120 s and the scanner after 900 s (exit 2). A very large changed file is
  slow to compare. A file near the scanner's size
  limit (3,000,000 bytes and 200,000 lines) exits 2, because the scanner would skip it.
- Diff mode reports lines added since the revision. A renamed file counts as new, so all its lines
  are reported. A deletion that leaves a new match behind (no line added) is not reported. A
  repository with no commits is scanned whole.
- Whole-path mode does not read `.gitignore`; diff mode does, so an ignored new file is not scanned.
  An explicit path that is not a Python file is skipped with "no Python files to check". One source
  file that is not UTF-8, or one unreadable directory, makes the whole run exit 2.
- An id in `default-rules.txt` that matches no rule exits 2. `install.sh` replaces the installed rule
  files and list and removes any other `*.yaml` in that directory. Keep custom rules in the
  repository copy, or in a directory named by `SLOP_CHECK_RULES`.
- Control characters in printed file names are escaped.
- `SLOP_CHECK_RULES` and `SLOP_CHECK_VENV` are trusted inputs: the lock in the rules directory decides
  what `slop-check --install` installs. Point them only at directories you control.
