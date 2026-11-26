# Configuration

[Home](../README.md) · [User guide](user-guide.md)

## Command-line settings

| Option | Default | Meaning |
|---|---|---|
| `--provider` | `codex` | `codex`, `newapi`, `llmmelon` use Codex CLI; `chat-completions` uses the compatible API and scoped file tools. |
| `--model` | `gpt-5.6-sol` | Model identifier passed to Codex. Select one your provider exposes. |
| `--reasoning-effort` | `xhigh` | Provider-supported effort; `none` omits explicit effort and disables reasoning summaries. |
| `--resource-variant` | model/effort lookup | `standard` or `weak`; explicit selection overrides the lookup on a new turn. |
| `--mode` | omitted | The first target-location call selects `proof` for direct proving, or `research` for mechanism exploration or uncertainty. Explicit `proof` / `research` overrides selection. A new `continue` turn also selects when omitted; `resume` keeps the saved mode. There is no `auto` value. |
| `--max-calls` / `--max-seconds` | unset | Soft total-call / cumulative active-time research budget, checked at dispatch boundaries; terminal delivery is separate. |
| `--max-proof-states` | 5 in proof; analysis-derived in research | Maximum successive proof loops for one target in proof; an upper bound on Orientation's allocation in research. A proof loop writes or revises one complete proof and independently checks it. |
| `--web-search-mode` | `on` | `on`: allow Survey retrieval only; `off`: disable it for every role. Blind probes retain their statement-only local context. |
| `--web-search-model` | primary model | Model for source-search calls. |
| `--web-search-reasoning-effort` | primary effort | Effort for source-search calls. |
| `--timeout-seconds` | `7200` | Shared timeout in seconds for core, Director, Survey, verifier, and writing calls. |
| `--output` | `outputs` | Root for new problem workspaces. |
| `--json` / `--profile` | off | Machine event output / timing records. |

In research mode, each Research or RCI dispatch consumes one proof state when reserved, including partial or mathematically failed attempts. RCI subphases and recovery share that single reservation. Prover assembly and repair, other roles, fixed Auditor/Kernel checks and reporting consume no proof-state credits. At zero credits, Prover can still use existing evidence; new Research or RCI work cannot start. All roles remain subject to the research-round, call and time limits. The cap does not limit total calls, tokens, time, or cost. Only research mode uses the validated difficulty and dependency levels to allocate the budget; an explicit cap can reduce it. Proof mode writes a short brief and uses its saved loop limit, with no research-budget assessment. Survey uses the same configured timeout as other roles. Director calls use low reasoning effort except when the configured effort is `none`. Only Survey may retrieve external sources when enabled. Roles preserve the exact source URLs and locators; retrieved material does not change the literal target or verification authority. Each Codex invocation pins search to live or disabled, rather than inheriting Codex defaults. Switching search off does not make model inference offline. A timed-out model call is stopped without automatic transport replay. After resolving the cause, `resume` retries the saved task with its existing budget and preserved partial artifacts. Operational failures pause before further model-based reporting; optional assessment content errors may use the default budget, while assessment transport failures remain resumable initialization steps.

Mode selection is committed with target extraction before budget allocation. Invalid routing receipts or service failures pause initialization without starting Survey, research or terminal writing. An uncertain but valid recommendation selects research. Batch `pipeline.mode` may be omitted or null; only explicit values emit `--mode`. Omitting batch `pipeline.web_search_mode` defaults to `on`, matching the CLI; set `off` to disable external retrieval.

`resume` restores the model, provider, reasoning effort, Survey settings and call timeout for the selected mathematical turn or writing request. Explicit CLI settings take precedence. A dedicated Survey model or effort is retained with the same provider; otherwise Survey follows the primary settings. Writing calls and writing time do not consume the mathematical turn’s call or time budget. See `pace resume --help` for its exact options.

Continued RCI work requires an exact existing question file from the dispatch's
input list. The Director may correct this reference once before dispatch; the
correction cannot change the action, inputs or mathematical task. Invalid
references remain diagnostics rather than reaching a specialist as file paths.

## Packaged resource defaults

These are project guidance choices, not an availability or performance claim:

| Model | Effort | Variant |
|---|---|---|
| `gpt-5.5` | `medium`, `high`, `xhigh` | `standard` |
| `gpt-5.6-sol` | `high`, `xhigh` | `standard` |
| `gpt-6.1-sol` | `medium`, `high`, `xhigh` | `standard` |
| `deepseek-v4-flash` | `none` | `weak` |
| Any unlisted exact pair | any | `weak` |

`weak` supplies more detailed role prompts. It is a resource bundle name, not an automatic measurement of a model's ability. New `standard` turns defer ordinary auxiliary applications and use a coverage-first Auditor. A documented structural failure can return a candidate for repair before Kernel review. A successful candidate always needs independent Kernel approval. `weak` keeps detailed manuscript and applicability checks and both whole-candidate reviews after a completed audit. Both variants use one [shared method library](../src/resources/skills/README.md), including the same mathematical checks and examples. Each run snapshots its selected prompts and the shared Skills under the turn's `resources/` directory. Both prompt variants and the full library ship in the wheel; resume keeps the saved snapshot.

On new `standard` turns the initial target extraction also supplies the short brief
and, for research, the budget assessment. Recovery can finish a missing part
from the committed target. Director normally sees the affected obligations and
latest handoffs; target stagnation or final assembly opens the wider dependency
view. Reporter still records every mathematical handoff and consults additional
premises on demand. A Mechanism Critic can return a specific local gap directly
to its author. A recurring gap on the same candidate and mechanism returns to
Conjecture Explorer. Each requested application still receives its own verdict.

Director and general research roles receive a compact Skill index and read the relevant entry and supporting material for their task. RCI loads methods for its specific phase; statement-only probes do not receive the general index or investigation history. Manuscript roles load the writing guides, while Auditor/Kernel retain independent verification prompts. Skills guide the mathematical work and do not grant a PASS.

## Authentication and custom providers

The simplest setup uses an already authenticated `codex` executable. The batch template has `authentication_mode: "existing_codex_login"` and empty API fields. `provider` does not configure an endpoint by itself.

PACE isolates each role call from personal Codex context: it reuses authentication while ignoring user configuration and disabling global memories, personal plugins, Apps, hooks, automatic subagents and standalone user Skills. Ordinary roles receive fixed instructions in a PACE-owned `AGENTS.md`; each task supplies its current inputs, outputs and budgets separately. The Director resumes its native session within the same turn, while specialists start fresh sessions. RCI phases retain their invocation-scoped environments. The Chat Completions transport includes the same fixed role instructions in its system message. Writing roles use their request's selected resource snapshot.

Settings apply only to the child process; no personal configuration files are edited. Use a recent Codex CLI supporting `codex exec --ignore-user-config`; `pace doctor` checks this locally. Custom endpoints must be supplied explicitly through the batch configuration below, rather than inherited from a personal Codex provider profile.

For a custom Responses-compatible service, copy `examples/quickstart/config.example.json` to the ignored `examples/quickstart/config.local.json`, then edit it:

- Set `codex.authentication_mode` to `api_key`.
- Set `codex.base_url`, `codex.api_key`, and a provider-supported `codex.model`/`reasoning_effort`.
- Set `codex.cli_path` to the real Codex executable and leave `codex.wire_api` as `responses`.
- Set `codex.provider_name` and `codex.requires_openai_auth` according to that service's Codex configuration.
- Set `pipeline.provider` as appropriate; the runner passes the connection settings through `scripts/codex_wrapper.sh`.

The wrapper uses `NL_PROOF_CODEX_API_KEY` in the child environment, not a literal key in the command arguments. Custom provider mode does not fall back to an unrelated existing login. No credentials belong in `config.example.json` or published snapshots.

Structured receipts still require the assigned JSON schema and routing action.
If an otherwise complete JSON response uses an unsupported escape inside a
string, PACE can preserve that backslash literally and revalidate the entire
object. The original response is retained, with a separate normalized copy and
recorded character offsets. It does not guess JSON structure, change actions or
reinterpret valid escapes; other malformed receipts use the bounded model
correction. This is an encoding correction, independent of network retries.

If Codex reports a code-mode tool-host negotiation timeout and produces no
usable receipt, PACE retries it within the existing transport retry limit.
A valid receipt remains usable and retains the tool-host warning. RCI checks
each phase's routing fields and required artifacts before committing its
checkpoint. A single receipt correction may fix missing control fields while
preserving valid decisions and original mathematical files.

The transport supports these environment variables:

| Variable | Purpose |
|---|---|
| `CODEX_CLI_PATH` | Executable used by PACE. The batch runner supplies its wrapper path. |
| `NL_PROOF_REAL_CODEX` | Real CLI executable used by the wrapper. |
| `NL_PROOF_CODEX_AUTH_MODE` | `existing_codex_login` or `api_key`. |
| `NL_PROOF_CODEX_BASE_URL`, `NL_PROOF_CODEX_API_KEY` | Custom provider endpoint and credential. |
| `NL_PROOF_CODEX_PROVIDER_NAME`, `NL_PROOF_CODEX_WIRE_API`, `NL_PROOF_CODEX_REQUIRES_OPENAI_AUTH` | Wrapper provider settings. |
| `NL_PROOF_CLI_TIMEOUT_SECONDS`, `NL_PROOF_CLI_MAX_RETRIES` | Transport timeout/retry overrides; retry count excludes the initial attempt. |

The batch runner sets these from its JSON config. For ordinary CLI use, shell variables must be exported explicitly; `.env` is not loaded automatically.

Native request and stream recovery allow 8 and 20 retries respectively. Outer transient retries are capped at two; `NL_PROOF_CLI_MAX_RETRIES` can lower that cap and excludes the initial attempt. Retry warnings do not cancel a running call. If retries are exhausted, PACE preserves the checkpoint and partial outputs. Correct authentication or configuration errors before using [resume](user-guide.md#resume-an-interrupted-turn) in the same problem directory.

The `chat-completions` provider defaults to eight retries per request.
`PACE_CHAT_MAX_RETRIES` accepts 0–20 retries, excluding the initial request.
Transient transport failures and HTTP 408, 429, 500, 502, 503 and 504 use
exponential backoff with jitter within the role's time limit. After three
consecutive failures, a short request checks the same endpoint and model
before PACE resends the full request. A failed probe pauses the role and
preserves its conversation and tool results for `resume`.
Authentication and other permanent errors stop immediately.

Each HTTP attempt records its own ID, elapsed time, failure stage, and
the server request ID when available in the turn's `events.jsonl`.

Ctrl-C stops the owned model/tool processes, preserves partial output and marks the call as interrupted; the CLI exits with code `130`. Parallel workers in that run are cancelled together; completed workers and reserved proof states are retained for `resume`. Other runs continue independently. Operational failures exit with code `2`. A normally completed research run exits with code `0`, even when the mathematical target remains unresolved. `--profile` records timing in the current turn's event log; a timing-write failure does not replace the task's result or interruption. Use `resume` in the same problem directory to continue saved work.

The Director receives separate live counts for proof states and research rounds.
An unset round cap does not exhaust the budget. The Host checks a budget-based
stop against those counts; a false budget stop triggers one correction of the
scheduling decision. If that correction fails, the scheduling checkpoint
remains resumable. An early stop for lack of an actionable task must state why.

## Writing tasks

The core exposes `outline`, `report`, `paper-outline` and `paper` over saved research evidence. Report and paper branches are independent. Each request saves the complete writing requirements, resource snapshot and selected outline version. `resume` continues the selected unfinished writing task, then a subsequent resume uses the mathematical checkpoint; writing status and PDF build status are separate from the mathematical `result`.

The Web application controls user-facing updates, reading copies and download packaging. Core completion does not depend on a generated reader brief.

## ReasAtlas source retrieval

ReasAtlas is optional. Its client and Skill are maintained separately and are
not included in the PACE repository, wheel, or source distribution. If you have
an authorized copy, keep it outside the PACE source tree and set:

```bash
export PACE_REASATLAS_SKILL_DIR="/absolute/path/to/reasatlas-query"
```

The directory must contain `SKILL.md` and `scripts/query.py`; its supporting
Markdown, JSON, Python and YAML files must be regular files. PACE copies those
files into the new turn's resource snapshot. Survey may retrieve sources when `--web-search-mode on` enables it. Without a
configured Skill, Survey uses its other retrieval tools. `off` disables
external retrieval even when the Skill is present.

Ordinary `resume` uses the saved snapshot; changing the environment does not
replace a turn's resources. New turns and explicit writing requests use the
current setting. Run snapshots may contain this separately supplied software;
keep them private unless you have permission to redistribute it. Web reader exports
omit resource snapshots.

Follow-up reads reuse the returned release ID. Survey saves the full JSON
record, source URL and locator with the report. Empty results or service errors
do not establish that a theorem is absent; applicability is checked in the
mathematical workflow. See the separately supplied Skill for client settings.

## Paper style

Paper writing snapshots the bundled `neurips_2026.sty` and uses its preprint mode. The source copies that style unchanged and omits a checklist and submission questionnaire. Paper prose defaults to English; an explicit writing request can select a different language. A Chinese problem statement or conversation alone does not change that default.

`latexmk -xelatex` builds the source and runs the bibliography tools when needed. `pdffonts` checks for Type 3 fonts. Chinese prose uses ctex and installed Fandol fonts. `pace doctor --pdf` checks the installed toolchain without model inference. Independent verifiers determine mathematical acceptance.

## Recovery and task scheduling

PACE isolates per-call Codex configuration while reusing authentication. Personal instructions, memories, plugins and Skills are excluded from automatic discovery. Packaged PACE resources are supplied explicitly. Partial outputs and original stderr are retained, and input mutations are restored before acceptance. Retry settings are described above.

Research, proof and writing have their own saved tasks. Runtime events contain factual identities, status and output references. The Web application reads these records to create user updates and reports; a presentation failure does not change the mathematical verdict or repeat completed work.

RCI checks the existing call/time budget before every new subphase and reserves at most 64 distinct new phase slots per dispatch. Recovery reuses committed phases. Mechanism Explorer starts a continued question with its original statement. A new conjecture is authored and independently reviewed before it becomes the active question.

RCI receipts carry only routing fields and artifact references. Review/change Markdown holds mathematical content. One receipt correction can fix missing controls without creating missing mathematics. A handoff returns original work and the remaining gap to the Director. A local advance can go to independent finding verification.

Scope-review reuse compares the actual question, target, review rules, recorded dependencies and objections. New or changed relevant evidence triggers a fresh review. Reuse is search context and does not authorize a final proof verdict.

Host-assigned mechanism identities and deduplication task identities are bound at receipt acceptance. Coverage selects promoted mechanisms, and deduplication returns the assigned tasks with valid predecessor references. A completed task saves its artifact and checkpoint before success is published. Ordinary recovery preserves that commit when later bookkeeping or external presentation is interrupted.


## Chat Completions API

This transport needs no Codex CLI. Set `PACE_API_BASE_URL` to a compatible `/v1`
endpoint and export `PACE_API_KEY` securely in the launching environment; neither
value is written into run config. The endpoint must support function tools.

```bash
pace run --provider chat-completions --model YOUR_MODEL --reasoning-effort none \
  --mode proof --problem examples/quickstart/EVEN_SQUARE.md \
  --problem-id api-smoke --output outputs --web-search-mode off
```

The model receives scoped `read_file`, `search_file`, `write_file` and `list_files` tools.
There is no shell or arbitrary code execution. Survey alone can `fetch_url` when
retrieval is on. This is a general Chat Completions transport, with no special
model-name routing. The requested effort is sent to the provider; its
interpretation depends on that provider. PACE adds no fixed tool-step count or
output-token cap; normal execution budgets, the configured timeout and provider
limits still apply. Network retries repeat
only the failed HTTP request, never tools already executed. The normal PACE
receipt checks, independent mathematical review and saved task recovery apply.
`pace doctor` checks the default Codex environment; it is not an API credential test.
Replace `YOUR_MODEL` with the endpoint's model identifier. `none` omits the optional
reasoning-effort parameter; select a supported effort when the endpoint accepts it.

`fetch_url` downloads the full source into the assigned Survey's
`sources/web_cache/`, retaining its URL, version bytes and SHA-256 hash.
HTML text preserves links and TeX math alternatives; PDF extraction uses the
installed [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)
dependency. The original remains available because extracted text can lose
mathematical layout; image-only PDF pages require another readable source.
`fetch_url` and `read_file` use the same HTML reading rules, including when opening
`artifact_ref`: retain prose, formulas, tables and links; omit CSS, fonts and
encoded image resources. Complete original bytes remain on disk. HTML extraction
does not execute JavaScript. A short static page is not evidence that it has no
further content: use `search_file` with `view: "embedded"` on `artifact_ref` to
locate data inside scripts or JSON, then use a match's `read_args` to read its
surroundings. The default `text` view provides document text. Coordinates refer
to the selected reading view, not raw HTML lines.

Responses default to 16,000 characters, with `next_read` providing the next
request, even inside a single long line. `max_chars` adjusts the response page
size; it is not a file-size acceptance limit. Line/page ranges are one-based;
`start_char`/`end_char` use zero-based character offsets with an exclusive end.
`fetch_url` initially selects 200 lines; `full_text: true` selects the entire
text, still returned in response pages. Follow `next_read` to obtain the rest.
No part of a mathematical manuscript is discarded because of its length.
Search uses literal keywords and returns character ranges, with optional
`case_sensitive`, `context_chars`, `max_results` and `next_search` pagination.
Search and reading enforce the same input permissions, including blind probes.
`list_files` retains optional `offset`/`limit` pagination.

Download caches are read-only to roles and remain at their original paths on
resume; only uncommitted interpretations and mathematical drafts are archived.
Terminal HTTP errors retain a credential-redacted response in
`provider_error.json` next to the call's `stderr.txt` for diagnosis.
Each failed network attempt records its status and elapsed time. Retries share
the configured call deadline. After retry exhaustion, `resume` can continue a
saved compatible-API conversation in the same task, with the same inputs and
permissions, without replaying completed file-tool calls. Changed inputs or
outputs invalidate that reuse; successful stages retain their own checkpoints.

The per-handoff Reporter is an ordinary model call and counts toward call/time
limits. It uses the main model configuration with external retrieval disabled.
A valid author handoff is saved before Reporter starts. Receipt correction is
bounded and cannot rewrite mathematical files; resuming an interrupted Reporter
does not spend another proof state or repeat the author. Long reports and papers
remain explicit writing tasks.
Reporter reference fields select exact paths from the frozen handoff manifest.
All invalid references are returned together. Reference correction reads only
the saved receipt and manifest, preserves the reported mathematics and outcome,
and cannot edit manuscripts. A saved receipt is reused on recovery; an accepted
receipt restores its projections without a new model call.

Within a handoff, proposals for the same obligation version and argument
manuscript share one independent application review. The request preserves all
exact premise bindings. Changing a proposal ID or reordering citations does
not trigger another review. A different target, obligation version, manuscript
or evidence set from a later handoff is checked separately. Existing review
requests and verdicts remain bound to their original evidence.
The Prover's own application of its whole candidate to the root target waits
for the independent Auditor and Kernel. After they accept the exact bound
candidate, the application points to their joint verification without another
model call. This does not close an open obligation or authorize a separate
premise. A failed candidate or a distinct application retains its own review.

Each model call executes its own saved prompt, including correction calls that
inherit task metadata. A sidecar correction reads its declared inputs and writes
only its assigned JSON; manuscripts remain protected and read-only. Route
references use plain file paths with optional `statement_locator` and
`scope_locator` fields. The Host can split an existing `file.md#anchor` locally
when that anchor is present in the original manuscript, retaining the original
JSON and repair record. Unknown anchors still require correction; they do not
authorize rewriting a manuscript or accepting a mathematical claim.
