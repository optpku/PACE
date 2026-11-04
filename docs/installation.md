# Installation

[Home](../README.md) · [User guide](user-guide.md)

## Requirements

- Python 3.11 or later. The runtime has no third-party Python dependencies.
- Git to clone the release branch below, or a downloaded ZIP of that branch.
- A macOS/Linux environment, or WSL on Windows. The core uses POSIX file locking (`fcntl`), and batch execution uses Bash and POSIX process-group management.
- A recent Codex CLI supporting `codex exec --ignore-user-config`, a configured sign-in, and access to the selected model. PACE calls `codex exec`; the Python installation does not install Codex. Custom providers use the explicit [batch configuration](configuration.md#authentication-and-custom-providers).
- Network access for model inference. Optional source retrieval can also use the network.

PACE currently invokes Codex with `--dangerously-bypass-approvals-and-sandbox` for unattended file-based tasks. Run it in an environment whose filesystem and command access you intend to grant to the agent. Its working directory is the individual problem workspace.

## 1. Install the local source

Clone the default `main` branch, which contains the PACE open-source edition:

```bash
git clone https://github.com/optpku/PACE.git
cd PACE
```

If you downloaded that branch as a ZIP, open its extracted directory containing
`pyproject.toml`. Check the selected Python, then install:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

If `python3` is older than 3.11, replace it with your installed supported interpreter. The distribution is named `pace-proof-agent`, but the import and executable are `pace`. Installation may download build tools such as setuptools; the installed base runtime has no third-party dependencies.

Alternatively, with `uv` already installed:

```bash
uv sync --locked --no-editable
source .venv/bin/activate
```

The implementation lives directly in `src/`; installation maps it to the
Python package `pace`. For source development with pip, use
`python -m pip install -e .`; with uv, `uv sync --locked` uses an editable install.
Run the CLI and helper scripts with that same
environment; adding `src/` to `PYTHONPATH` is not an installation step.

## 2. Prepare Codex CLI

If Codex is not installed, follow the [official Codex CLI installation guide](https://developers.openai.com/codex/cli). One supported method, when npm is already installed, is:

```bash
npm install -g @openai/codex
```

Check it from the same shell that will run PACE:

```bash
codex --version
codex login status
```

If sign-in is needed, run `codex login` and complete the CLI's interactive instructions. PACE reuses the login while isolating its role calls from personal memories, plugins and directory instructions. For a custom executable, export `CODEX_CLI_PATH` with its absolute path. For a custom Responses-compatible provider, follow [Configuration](configuration.md).

PACE does not load `.env` automatically. Export optional shell variables as described in [Configuration](configuration.md#authentication-and-custom-providers). No key is needed for the local installation checks below.

<a id="paper-pdf-tools"></a>

## 3. Prepare paper PDF tools when needed

Automatic terminal paper delivery and explicit `paper` tasks require `xelatex`, `bibtex`, and `pdffonts` on PATH. Install a [TeX distribution](https://tug.org/texlive/) and [Poppler](https://poppler.freedesktop.org/). The bundled NeurIPS preprint style needs its LaTeX dependencies, including T1 fonts and courier; Chinese prose uses ctex and Fandol fonts.

```bash
xelatex --version
bibtex --version
pdffonts -v
```

The writer keeps `main.tex`, any bibliography and local assets together. PACE builds in a separate directory, checks unresolved references and Type 3 fonts, and records source delivery separately from PDF build status. These checks do not certify the mathematics.

If tools are unavailable or a build fails, the source remains saved. Repair the toolchain and use `pace resume --run-dir PATH` to retry the selected paper task without rewriting an already committed manuscript.

## 4. Verify the installation without inference

Keep the PACE environment active and use the same shell in which Codex is available:

```bash
python -c "import pace; print(pace.__version__)"
pace --help
pace run --help
pace doctor
pace problems --output outputs --json
```

These commands do not call a model. `pace doctor` checks Python, POSIX utilities and Codex compatibility. An executable that exists but cannot run, or exits without a recognizable version, fails the check. These checks do not verify model access or service availability.

Before running the quickstart, also check the PDF toolchain:

```bash
pace doctor --pdf
```

This compiles a small document with formulas, cross-references and a bibliography,
then inspects its fonts. Its several LaTeX/BibTeX passes may take a few minutes.

An empty problem list is expected before your first run. Proceed to the example after the environment checks pass.

## 5. Run the included example

From the repository root, follow the [quickstart example](../examples/quickstart/README.md). It makes model calls, so choose a model and reasoning effort available to your account. Read `work/turn001/outcome.md` and `work/turn001/final_report.json`, then use the [user guide](user-guide.md) to investigate your own conjecture, continue a topic, or resume an interrupted turn.

## Troubleshooting

| Symptom | Action |
|---|---|
| Clone reports `Repository not found` or permission denied | Confirm the URL and that your GitHub account can access the repository; GitHub access is separate from Codex sign-in. Download the `main` branch. |
| The checkout lacks the documented `pace` command or layout | Update to the current `main` branch, then install from its directory containing `pyproject.toml`. |
| `pace: command not found` | Activate `.venv`, or run `.venv/bin/pace`; confirm the local package is installed. |
| `ModuleNotFoundError: pace` | Use the Python interpreter in the environment where PACE was installed. |
| `ModuleNotFoundError: sympy` in an agent tool call | Use the absolute Python path in the call's execution capabilities; a nested shell's `python3` may belong to another environment. Install `.[math]` into the PACE environment if it is actually missing. |
| `No module named fcntl` | Run PACE under macOS/Linux/WSL. |
| `cli_not_found` | Make `codex` available on PATH, or set `CODEX_CLI_PATH`. |
| `codex_isolation` check fails or `--ignore-user-config` is unrecognized | Update Codex CLI, then rerun `pace doctor`; do not remove the isolation flags. |
| Login/provider/model error during a run | Check Codex configuration and model access in the same shell. Configuration checks do not verify service availability. |
| Retry or stream-recovery warning | Allow the bounded recovery attempt to finish; the warning alone does not cancel the call. See [retry settings](configuration.md#authentication-and-custom-providers). |
| Transient service retries exhausted | Resolve the service or connection issue, confirm the previous process has stopped, then [resume the saved turn](user-guide.md#resume-an-interrupted-turn) in the same directory. |
| Initial Orientation/setup failed | Correct the environment or service problem, then use `resume` in the same directory. Committed initialization results are reused. A changed problem statement requires a new turn or problem ID. |
| Problem directory already exists | Use `continue` for a new turn or `resume` for an unfinished turn, or choose a new problem ID. |
| Paper build is `FAILED` or `UNAVAILABLE` | Read `build.json` and `build.log`; verify `xelatex`, `bibtex`, `pdffonts`, fonts and required packages, then resume the paper task. |
| Tool versions pass but `doctor --pdf` fails | Repair the TeX installation and required packages, then rerun the compilation check. Base classes, styles and fonts must be readable local files; cloud placeholders or an incomplete distribution can pass a version check and still fail compilation. |
| Missing prompt/Skill after packaging | From a complete source directory, run `python -m pip install --force-reinstall .`; installed resources belong under `pace/resources/`. |

## Optional mathematical tools

The base package has no third-party runtime dependencies. From the source
directory, install SymPy and mpmath with the same tool used for PACE:

```bash
# pip environment
python -m pip install '.[math]'
```

```bash
# uv environment; retain --extra math on later syncs
uv sync --locked --no-editable --extra math
```

An environment created by uv may not include pip. PACE supplies the current
Python executable and optional tool availability to roles; missing optional
packages do not imply that a theorem is false. Both modes complete mathematical work before automatic manuscript delivery;
missing TeX pauses only PDF delivery.
