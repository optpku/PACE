# User guide

[Home](../README.md) · [Configuration](configuration.md)

## Before you start

Complete [installation and environment checks](installation.md): obtain the `main` source branch, install PACE, configure Codex, and run `pace doctor`. The proof-mode example below needs no PDF tools. For explicit paper writing, prepare the PDF tools and also run `pace doctor --pdf`. The commands below assume the repository root and an active PACE environment.

## Write a problem

Use a UTF-8 Markdown or text file. State the mathematical objects, assumptions, quantifiers, and the exact conclusion. Explain whether you want a proof of a fixed statement or an exploration of a family of questions. Specify the allowed outcomes, such as a proof, counterexample, construction, or conditional result, and which refinements of the question are permitted. Include definitions, relevant source statements, known special cases, and any approaches or obstructions already identified. A PDF is not a direct `--problem` input; extract the relevant text first.

A self-contained starting point is [the parity example](../examples/quickstart/EVEN_SQUARE.md). The persistent problem directory is the place for follow-up materials and prior evidence. A reference to a local file should identify where the file can actually be read; `input_refs` in the implementation are reading suggestions, not permission boundaries.

## Start a new problem

From the repository root, with your environment active:

```bash
pace run \
  --problem examples/quickstart/EVEN_SQUARE.md \
  --problem-id even-square --output outputs \
  --model gpt-6.1-sol --reasoning-effort high \
  --mode proof \
  --web-search-mode on
```

Replace the model and effort with values supported by your provider. This creates `outputs/even-square/` and starts `turn001`. `proof` handles a fixed statement with one initial Survey followed by direct proving. It runs at most five successive proof loops by default: the first writes a complete proof and later loops revise it using independent review feedback. Verification success ends the turn early; there is no candidate pool. Repairs and resume reuse the Survey. `research` uses dynamic Director scheduling with Planner, Research and SMI as needed. Omit `--mode` to let the first target-location call select: only tasks suited to direct proving choose proof; mechanism exploration or uncertainty chooses research. Explicit mode choices override selection. Both modes use independent Auditor/Kernel checks; see [mode and budget settings](configuration.md#command-line-settings).

For your own open conjecture, save the problem as `conjecture.md` and start a research-mode run:

```bash
pace run \
  --problem conjecture.md \
  --problem-id my-conjecture \
  --output outputs \
  --mode research \
  --model gpt-6.1-sol --reasoning-effort high \
  --web-search-mode on
```

This creates `outputs/my-conjecture/`. In the continuation and recovery commands below, replace `outputs/even-square` with `outputs/my-conjecture` when working on this study.

Web Search is enabled by default for every role when its assigned task needs external sources. Use `--web-search-mode off` to disable external retrieval. Statement-only probes retain their restricted local inputs.

The terminal prints factual task milestones, errors and artifact paths. The external Web application generates user updates, reader briefs and activity cards from those records. `--json` emits machine-readable started/completed JSONL records. `--profile` includes timing events. The model may produce partial findings even if the full target remains unresolved.

## Read results

```bash
pace problems --output outputs
pace problems --output outputs --json
```

Read `work/turnNNN/outcome.md` for the factual outcome and `work/turnNNN/final_report.json` for the recorded mathematical result and evidence references. Read `result` with `answer_mode` and the original verifier records. `run_status` describes execution separately from mathematical acceptance. Auditor and Kernel provide model-based natural-language review.

`results/findings/README.md` and `archive.md` index the independently checked manuscripts. Original findings and their exact verification scope accumulate across turns. Rejected drafts remain outside that archive and their review records remain available to the Director for repair.

PACE records mathematical originals and runtime events. The Web application generates the user-facing brief, thematic results overview, activity cards, reader copies and downloads. These presentation documents are not required inputs for core execution or recovery. Explicit report and paper tasks are described below.

The core stores `orientation/`, `round1/`, `round2/` and `finalization/` directly
under each `work/turnNNN/`. SMI candidates are direct children of `smi/attempt_NNN/`;
each phase writes its files directly in a named directory, such as
`candidate_01/seed_scout/seed.md`. Distinct phase iterations use sibling names
such as `frontier_explorer/` and `frontier_explorer_02/`. Internal attempts remain
immutable for resume. Core reader-path rules remove the attempt directory and
the redundant `reports/round-NNNN/` container, so a report snapshot appears as
`work/turn001/round3/current_report.md`. Reader publication selects the last
successful committed attempt; a later failure does not replace it. Seed manuscripts
are published directly as `round4/seed.md`; other candidates and seed revisions use
names such as `seed-candidate_02.md` and `seed-revision_02.md` to preserve distinct
manuscripts without extra directories. The original files remain available for resume. Web uses
these core rules when rendering and placing the reading files.

## Continue with a new question

After the current turn has ended, write your next research question in a UTF-8 file named `followup.md`, then add it to the topic:

```bash
pace continue \
  --run-dir outputs/even-square \
  --message followup.md \
  --model gpt-6.1-sol --reasoning-effort high \
  --web-search-mode on
```

This starts the next numbered turn in the same problem directory. Earlier turn artifacts and accumulated findings remain available, while each turn records its own outcome and runtime facts. State the new target explicitly; a previous accepted target does not settle the new one.

Each new `continue` turn selects its mode from the current request when `--mode` is omitted, preserving prior work as context. Add `--mode research` or `--mode proof` to choose explicitly. `resume` does not make a new selection. Select models and search settings appropriate to the new turn.

## Resume an interrupted turn

Stop the previous Host and its owned model/tool processes before resuming. The CLI and Python API use the same single-writer lock. A surviving child process must stop before another writer can acquire the workspace.

```bash
pace resume --run-dir outputs/even-square \
  --model gpt-6.1-sol --reasoning-effort high --web-search-mode on
```

`resume` continues the selected saved task. It preserves the target, mode, resource snapshot, proof-loop limit and committed outputs. `--resource-variant`, `--max-proof-states` and `--mode` are not resume options. Completed mathematical work is reused without another model call. If a writing task is selected, recovery continues that writing task without restarting research.

A service failure retains its concrete diagnostic and stops subsequent model dispatches in that Host session. Correct the service or environment before resuming. A new literal question uses `continue` or a new problem ID, preserving the committed original.

At a saved research Director boundary, `pace resume --run-dir PATH --finalize` explicitly closes research with the current findings. It does not grant whole-target mathematical acceptance. Pending mathematical work or verification must first reach a resumable boundary.

A completed artifact and its owning task checkpoint are saved before call success is published. Later log or Web presentation failures do not repeat that completed task. Incomplete attempts and their diagnostics remain in the turn directory.

## Write a report or paper

Writing uses two independent paths over the run's existing evidence. Supply the complete writing request in `writing-request.md` and optional additional requirements in a UTF-8 file.

```bash
pace outline --run-dir outputs/even-square --message writing-request.md
pace report --run-dir outputs/even-square --outline results/outline.md

pace paper-outline --run-dir outputs/even-square --message writing-request.md
pace paper --run-dir outputs/even-square --outline results/paper_outline.md
```

Each command accepts execution settings such as `--model`, `--reasoning-effort` and `--web-search-mode`. Add `--requirements FILE` for additional requirements. Report and paper writing save the selected outline as a read-only snapshot together with the original request. The paper path can use the existing research directly.

Each step uses one author task with its Composer provenance, saved output and delivery/format checks. Writing preserves mathematical targets, evidence and verifier decisions. It cannot fill a missing mathematical proof or turn an unfinished target into PASS. A new request starts a new writing attempt; `pace resume --run-dir PATH` continues the selected unfinished attempt.

Reports are written to `results/research_report.md`. Paper sources and their local dependencies are written under `results/paper/`, with a successful build at `results/paper/main.pdf`. Paper prose defaults to English unless the user explicitly requests another language. The saved NeurIPS preprint template is used without a checklist or submission questionnaire.

PDF compilation uses XeLaTeX, BibTeX when cited, and `pdffonts`. The source delivery and build status are separate. Build failures keep the source and diagnostic, and recovery can retry compilation without calling the author again. Writing and build status do not alter the committed mathematical verdict. The external Web application organizes reader copies and download archives.

## Run a batch

The batch templates reuse the same parity input as the quickstart. Copy the configuration before changing local settings:

```bash
cp examples/quickstart/config.example.json examples/quickstart/config.local.json
python scripts/run_batch.py --suite-profile examples/quickstart/suite.json --check
python scripts/run_batch.py --suite-profile examples/quickstart/suite.json --dry-run
```

`--check` validates configuration and executable/credential presence, not network connectivity or model access. `--dry-run` shows the planned commands without inference. Inspect the selected problem IDs, model, output paths, and parallelism. Then run:

```bash
python scripts/run_batch.py --suite-profile examples/quickstart/suite.json --ids EVEN_SQUARE
```

This last command makes model calls. Relative paths in the config resolve from the config file's directory. The supplied template selects `EVEN_SQUARE` and places its workspace under `examples/quickstart/outputs/EVEN_SQUARE/`. It runs one problem at a time, resumes unfinished research, recoverable operational failures, and selected unfinished writing tasks when possible, and skips completed core tasks. For your own suite, update the input paths, ID pattern and selection in copies of the templates. See `--help` for `--limit`, `--parallel`, `--no-resume`, and progress controls.

Interrupting a batch stops its active process trees and cancels queued problems.
If a selected writing task remains unfinished, the batch records
`status=writing_incomplete` separately from the mathematical `result`. Correct
the writing/build problem and resume the saved task. Web presentation state is
not used to decide whether a core run should repeat.

For an existing runner artifact directory, `pace postmortem --batch-dir PATH --inactive` writes a local `_runner_summary.json`; use `--active` when an external runner is still active. This is a diagnostic rescan, not a new proof or an independent verifier. For the current problem-workspace layout, use the result-summary script below.

## Summarize recorded results

```bash
python scripts/summarize_results.py --runs-dir outputs --output exports/summary
```

The destination must be a new directory outside the source directories. This command reads the current turn of each immediate child problem directory and writes factual `summary.json` and `README.md` without model calls or source changes. Preserve the original workspace for recovery. Reader-view exports, file-link conversion and download packaging belong to the external Web application.

An SMI assignment states the fixed question, the one gap to address and its return condition. Continuing an unchanged question enters exploration directly. A bounded attempt can return `HANDOFF` with its original work and unresolved gap; this is distinct from a mathematical rejection.
