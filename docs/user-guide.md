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

Only Survey may retrieve external sources; `--web-search-mode on` enables Survey retrieval and `off` disables it. Other roles have no retrieval override. Statement-only probes retain their restricted local inputs.

The terminal prints factual task milestones, errors and artifact paths. The external Web application generates user updates, reader briefs and activity cards from those records. `--json` emits machine-readable started/completed JSONL records. `--profile` includes timing events. The model may produce partial findings even if the full target remains unresolved.

## Read results

```bash
pace problems --output outputs
pace problems --output outputs --json
```

Read `work/turnNNN/final_report.json` for the recorded mathematical result and evidence references. Read `result` with `answer_mode` and the original verifier records. `run_status` describes execution separately from mathematical acceptance. Auditor and Kernel provide model-based natural-language review.

`results/findings/README.md` and `archive.md` index the independently checked manuscripts. Original findings and their exact verification scope accumulate across turns. Rejected drafts remain outside that archive and their review records remain available to the Director for repair.

PACE records mathematical originals and runtime events. The Web application generates the user-facing brief, thematic results overview, activity cards, reader copies and downloads. These presentation documents are not required inputs for core execution or recovery. Explicit report and paper tasks are described below.

The core stores `orientation/`, `round1/`, `round2/` and `finalization/` directly
under each `work/turnNNN/`. RCI keeps original manuscripts in numbered
phase directories such as `rci/attempt_001/001_conjecture_explorer/candidate.md`
and `rci/attempt_001/004_mechanism_critic/review.md`. The round's research
handoff links the active question, latest evidence and committed phase files.
Web reader links point to those originals. Core resumes from the saved phase
checkpoints.

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

`resume` continues the selected saved task. It preserves the target, mode, resource snapshot, proof-loop limit and committed outputs. `--resource-variant`, `--max-proof-states` and `--mode` are not resume options. Completed mathematical work is reused without another model call. An unfinished writing request takes priority, including a paper awaiting its PDF. After delivery, resume returns to the saved mathematical checkpoint.

Resume requires a resource snapshot with the current workflow and verification protocol. To resume an experiment with an older snapshot, use the version of PACE that created it.

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

Each command accepts execution settings such as `--model`, `--reasoning-effort` and `--web-search-mode`. Add `--requirements FILE` for additional requirements. Report and paper writing freeze the selected outline. The current PACE outline carries its original instructions. For a standalone outline, supply its instructions with `--message`. Requesting a new outline keeps the last delivered report or paper available. The paper path can use the existing research directly.

Each step uses one author task with its Composer provenance, saved output and delivery/format checks. Writing preserves mathematical targets, evidence and verifier decisions. It cannot fill a missing mathematical proof or turn an unfinished target into PASS. A new request starts a new writing attempt; `pace resume --run-dir PATH` continues the selected unfinished attempt.

Reports are written to `results/research_report.md`. Paper sources and their local dependencies are written under `results/paper/`, with a successful build at `results/paper/main.pdf`. Paper prose defaults to English unless the user explicitly requests another language. The saved NeurIPS preprint template is used without a checklist or submission questionnaire.

PDF compilation uses `latexmk -xelatex` to manage compilation and bibliography passes, then checks fonts with `pdffonts`. PACE records source delivery and PDF build status separately. When a replacement fails to compile, the last successful PDF stays in `results/paper/`. The command returns the new attempt’s source path and build status. Resume can retry compilation from that source without calling the author again. The committed mathematical verdict comes from independent verification. The external Web application organizes reader copies and download archives.

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


## Research memory and task context

New turns record `research_memory_version=1` and `research_delta_owner=reporter`. Each task freezes required inputs,
on-demand archives and its route version under `roundN/assignment/`. Resume checks
those versions before continuing; later route edits do not change saved inputs.
Director can dispatch a clear next task under the root target or a registered
obligation. Planner is called for shared dependencies or consequential route
choices. It delivers a compact route and relations, and the parent commits its
shared pointer.

Required reading and on-demand archives have separate lists, but both grant
read-only access through the compatible API's file tools. The Director can follow
the Host's problem and memory indexes; task roles use their saved archive lists.
Public findings link to their exact manuscripts and reviews across turns. An
arbitrary link in a manuscript does not grant additional access, and blind probes
retain only their supplied statement and permitted assumptions.

`memory/research_index.json` links target obligations, scoped attempts, proposed
applications and independent reviews. It is a rebuildable search index, never a
substitute for the source proof. A checked local theorem and its applicability
to the current obligation are separate. Application reviews may accept a use
without creating another result number. After three mathematical attempts without progress on the literal target, the
Director considers the missing bridge. Local results reset the counter for the
specific subproblem. A checked use for the target or one of its declared
requirements, or an evidence-backed revision of a required dependency, resets
the target counter. Missing-input attempts do not count. Planner receives the
same assessment frozen at dispatch and identifies the next inference needed
for the original question. A standard Planner work order identifies that gap,
its direct premises and the next task. Stopping criteria are optional.

For `standard`, an auxiliary lemma can be used provisionally in the next
proof. The Host queues applications for open target requirements,
or for assigned obligations in a specific `REASSESS` dispatch. Other proposed
uses remain in the original handoff. An independently useful standalone paper
result or an explicit request for a manuscript check can still be nominated.
The Auditor focuses on coverage and can return a documented structural failure
to the author. The Kernel then checks the revised candidate before acceptance.
On new standard turns its mathematical receipt contains the verdict, a short
reason (`summary`) and the relevant passage (`proof_location`). A PASS also
requires explicit publication eligibility and a findings action. The Host uses
a failed judgment directly for repair. Method cards and historical sources
are available on demand. A separate reusable lemma requires its own scoped
authorization. If publication fields are missing or an UPDATE references an
unknown finding, a correction uses the saved mathematical judgment and repairs
only the affected controls. The Host drops malformed optional lemma exports
while retaining the valid verdict and any independently authorized exports.

For `weak`, complete auxiliary results and proposed uses keep their detailed
independent checks. The verifier reads the original and supporting evidence and
judges each use under its own premises. Auditor and Kernel both review the
whole candidate after a completed audit, including an audit that finds a gap.
Both variants reuse the decisions already completed for the same bound scope.

After each successful outer Research, Prover or RCI task, Reporter reads the
saved handoff and direct dependencies once. It returns a short summary and a
structured delta; the Host writes `roundN/progress.md` and
`roundN/research_delta.json`. Reporter does not write proofs, retrieve sources,
or authorize acceptance. Intermediate derivations can inform the next task as
provisional evidence. Reporter nominates independently useful complete results
and applications ready to close a registered obligation. The finding verifier
reads the original and its direct evidence, gives a separate verdict for each
proposed use, and returns concise reasons. The Host records exact versions and
projects the readable review for a compact receipt. A cited current finding is resolved to its original
and review. A task that received an earlier snapshot keeps that version. For
a standard-mode check with an authorized, version-bound verdict with a checked statement, the verifier
receives the statement and conditions directly and can open the evidence on demand. If a joint review finishes
the applications but the manuscript check is interrupted, a manuscript-only
retry uses its saved evidence and preserves the application decisions. RCI
subphases do not each call Reporter. Planner writes `route_relations.json`
for dependency changes. In a local revision it can declare
`relations_unchanged` in the patch so the Host records an empty change.
Survey does not trigger this progress call.
Proof mode also reports after each Prover handoff, before independent verification.
Its progress receipt leaves applications empty: the existing whole-proof verifier
checks the candidate, so no unused application-review queue is created.
This short task is separate from explicit long-report and paper writing.

`reporter_request.json` freezes its input references and versions;
`reporter_result.json` saves the accepted receipt before projections are written.
If Reporter is interrupted, resume keeps the completed author and finishes only
the missing reporting step. Missing progress projections and the derived memory index are restored on resume,
including completed turns, without a model call. Changed original evidence, the bound target or an obligation statement is reported
as a version conflict. Navigation snapshots cannot serve as mathematical premises. Parallel tasks have separate receipts; only their parent
updates shared memory. `memory/README.md` links the latest task progress.

Resume older runs that assigned deltas to mathematical authors with their original PACE runtime.

Planner can mark an unused route obligation `DEFERRED` or `SUPERSEDED`, with its reason in the route manuscript. This does not retract historical evidence. Splitting a task retains its ancestor obligations for obstacle lookup and reassessment; local progress alone does not close the parent target.
