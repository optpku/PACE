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

Replace the model and effort with values supported by your provider. This creates `outputs/even-square/` and starts `turn001`. `proof` handles a fixed statement with one initial Survey followed by direct proving. It runs at most five successive proof loops by default: the first writes a complete proof and later loops revise it using independent review feedback. Verification success ends the turn early; there is no candidate pool. Repairs and resume reuse the Survey. `research` uses dynamic Director scheduling with Planner, Research and RCI as needed. Omit `--mode` to let the first target-location call select: only tasks suited to direct proving choose proof; mechanism exploration or uncertainty chooses research. Explicit mode choices override selection. Both modes use independent Auditor/Kernel checks; see [mode and budget settings](configuration.md#command-line-settings).

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

Only Survey has external retrieval enabled by default. Use `--web-search-mode off` to disable Survey retrieval; no option enables retrieval for other roles. Statement-only probes retain their restricted local inputs.

The terminal prints factual task milestones, errors and artifact paths. The external Web application generates user updates, reader briefs and activity cards from those records. `--json` emits machine-readable started/completed JSONL records; `navigation` links the run entry page. `--profile` includes timing events. The model may produce partial findings even if the full target remains unresolved.

## Read results

```bash
pace problems --output outputs
pace problems --output outputs --json
```

Open the run `README.md`, then the current turn and round pages. `work/turnNNN/final_report.json` records the mathematical result and evidence references. Read `result` with `answer_mode` and the original verifier records; execution status is separate from mathematical acceptance.

`results/findings/README.md` is the thematic entry point; `catalog.md` lists current numbered results and `archive.md` links exact checked versions. Original findings and their exact verification scope accumulate across turns. Rejected drafts remain outside that archive and their review records remain available to the Director for repair.

PACE records mathematical originals and runtime events. The Web application generates the user-facing brief, thematic results overview, activity cards, reader copies and downloads. These presentation documents are not required inputs for core execution or recovery. Explicit report and paper tasks are described below.

### Experiment files

New runs record `pace_run_layout/v2` in `work/state/layout.json`. Existing
experiments are not moved: their bound runtime and original evidence paths remain
in use. Keep the whole run directory, including `work/`, for recovery.

```text
<run>/
├── README.md
├── results/
│   ├── findings/
│   ├── outline.md
│   ├── research_report.md
│   ├── paper_outline.md
│   └── paper/{main.tex, main.pdf, ...}
├── memory/
└── work/
    ├── state/
    ├── runtime/
    └── turn001/
        ├── README.md
        ├── input/, state/, resources/
        ├── events.jsonl
        ├── final_report.json
        ├── orientation/
        ├── round0001/
        ├── round0002/
        │   ├── README.md
        │   ├── task.json, context.json, route_context.json
        │   ├── agent_result.json, research.md
        │   ├── findings/, verification/, calls/
        │   └── rci/attempt_001/
        │       ├── outcome.json
        │       ├── semantic_dedup/
        │       └── candidate_01/
        │           ├── seed_scout/
        │           ├── frontier_explorer/
        │           └── frontier_explorer_02/
        └── finalization/
            ├── README.md
            ├── report-outline-001/
            ├── report-001/
            ├── paper-outline-001/
            └── paper-001/
```

Auxiliary files are omitted. Each `roundNNNN` is a dispatch round, distinct from a
user turn and from the proof-state budget. Proof mode uses one directory per
successive proof loop; verification stays beside that loop's proof. RCI attempt,
candidate and phase directories denote different attempts, statements and phases;
`frontier_explorer_02` is a later mathematical iteration. Transport retries stay
in `calls/`. Parallel workers use distinct, pre-reserved round directories.

Research assignments freeze `route_context.json` (route version, work item and
sources) and `context.json` (required inputs and on-demand archives) before the
worker starts. Resume uses those saved references, including for parallel tasks.
Blind probes continue to receive only their independent input view; adjacent
navigation and candidate files do not become permitted context.

The Host updates the run, turn and affected round pages from saved records.
Pages contain facts and links, not copied proofs or model-written summaries.
They are not acceptance or recovery checkpoints. Rebuild missing pages locally:

```bash
pace index --run-dir outputs/even-square
```

A stable result such as `R0001` may acquire a new independently checked version.
The verifier selects `CREATE` for a distinct result, `UPDATE` for an existing
result, or `NONE` for checked supporting material that needs no new number.
Original manuscripts and reviews remain immutable in `work/`; the archive keeps
all versions, including supporting material. Saved dependencies retain their
exact versions, and route memory detects a changed version of an existing ID.
These publication choices do not change mathematical acceptance. An invalid
publication field is corrected separately without rerunning a completed check.

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

`resume` continues the selected saved task. It preserves target, mode, resources, proof budget and committed outputs. Mode and budget are not resume options. An unfinished explicit writing task completes first, then recovery continues the same turn’s paused proof/research checkpoint. Completed writing and review stages are reused; completed mathematics does not trigger writing.

Every new run saves its executable package under `work/runtime/initial/pace` and
binds its file hashes in `work/state/runtime_binding.json`. For a bound run, CLI resume, continue
and writing commands first verify the package hashes and use that runtime.
Keep its `work/runtime/` directory when moving the run. Historical identifiers,
prompts, manuscripts and reviews remain unchanged; new runs use RCI throughout.

A service failure retains its concrete diagnostic and stops subsequent model dispatches in that Host session. A turn resumed after explicit writing is marked complete only after the Host session exits successfully; a terminal service failure leaves it interrupted. Correct the service or environment before resuming. A new literal question uses `continue` or a new problem ID, preserving the committed original.

At a saved research Director boundary, `pace resume --run-dir PATH --finalize` explicitly closes research with the current findings. It does not grant whole-target mathematical acceptance. Pending mathematical work or verification must first reach a resumable boundary.

A completed artifact and its owning task checkpoint are saved before call success is published. Later log or Web presentation failures do not repeat that completed task. If the final delivery notice is missing, `resume` publishes it from the saved artifacts without repeating writing, review or PDF compilation, including for older completed checkpoints. Incomplete attempts and their diagnostics remain in the turn directory.

## Write a report or paper

Research ends without starting long-form writing. Request the commands below when a report or paper is wanted. Each requested report/paper has independent review and necessary revisions. `resume` retains accepted authorship and reviews. Missing tools pause PDF delivery; concrete compiler source errors trigger local revision and review before rebuilding.

Writing uses two independent paths over the run's existing evidence. Supply the complete writing request in `writing-request.md` and optional additional requirements in a UTF-8 file.

```bash
pace outline --run-dir outputs/even-square --message writing-request.md
pace report --run-dir outputs/even-square --outline results/outline.md

pace paper-outline --run-dir outputs/even-square --message writing-request.md
pace paper --run-dir outputs/even-square --outline results/paper_outline.md
```

Each command accepts execution settings such as `--model` and `--reasoning-effort`. Writing roles always keep retrieval disabled, including when the shared `--web-search-mode on` option is supplied. Add `--requirements FILE` for additional requirements. Report and paper writing save the selected outline as a read-only snapshot together with the original request. The paper path can use the existing research directly.

Each request owns one task directory under `work/turnNNN/finalization/`: `report-outline-001`, `report-001`, `paper-outline-001` or `paper-001`. Its `request.md`, `selected_outline.md`, drafts, reviews, revisions and `state.json` remain together. A new user request increments the task number; restart and resume do not. The turn’s writing pointer selects that task. A writing command alone does not advance paused mathematics; subsequent `resume` can finish writing and continue its saved mathematical workflow. Formatting repairs first use conservative local rules, then constrained patches at fixed `medium` effort; independent editorial review remains required.

The latest completed reading copies are `results/outline.md`, `results/research_report.md`, `results/paper_outline.md` and `results/paper/`. A report under revision or failed PDF build does not replace the previous successful delivery. Commands return exact historical version paths. A paper task keeps its initial complete source bundle in `draft/`, later bundles in `revision-001/`, and independent reviews beside them as `review-001.md`. Each accepted paper bundle is independently compilable. Default paper language is English; the NeurIPS preprint style is retained without a checklist.

PDF compilation uses XeLaTeX, BibTeX when cited, and `pdffonts`. The source delivery and build status are separate. Build failures keep the source and diagnostic. Environment or unclassified build failures pause for inspection. Recognized source errors trigger local patches, review and recompilation until the PDF builds. Reports and papers have no fixed revision-count limit. The first review collects the required corrections; follow-up reviews check unresolved issues and the impact of changes, and may identify new substantive defects. Empty or cyclic patches and persistent identical compiler errors receive a focused correction before a resumable pause. Ordinary revisions continue automatically, with completed writing and reviews retained across recovery. Optional style preferences do not block delivery. Writing and build status do not alter the committed mathematical verdict. The external Web application organizes reader copies and download archives.

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

An RCI assignment states the fixed question, the one gap to address and its return condition. Continuing an unchanged question enters exploration directly. A bounded attempt can return `HANDOFF` with its original work and unresolved gap; this is distinct from a mathematical rejection.
