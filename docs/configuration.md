# Configuration

[Home](../README.md) · [User guide](user-guide.md)

## Command-line settings

| Option | Default | Meaning |
|---|---|---|
| `--provider` | `codex` | `codex`, `newapi`, or `llmmelon`; all use the Codex CLI transport. |
| `--model` | `gpt-5.6-sol` | Model identifier passed to Codex. Select one your provider exposes. |
| `--reasoning-effort` | `xhigh` | Provider-supported effort; `none` omits explicit effort and disables reasoning summaries. |
| `--resource-variant` | model/effort lookup | `standard` or `weak`; explicit selection overrides the lookup on a new turn. |
| `--mode` | omitted | The first target-location call selects `proof` for direct proving, or `research` for mechanism exploration or uncertainty. Explicit `proof` / `research` overrides selection. A new `continue` turn also selects when omitted; `resume` keeps the saved mode. There is no `auto` value. |
| `--max-calls` / `--max-seconds` | unset | Soft total-call / cumulative active-time research budget, checked at dispatch boundaries; terminal delivery is separate. |
| `--max-proof-states` | 5 in proof; analysis-derived in research | Maximum successive proof loops for one target in proof; an upper bound on Orientation's allocation in research. A proof loop writes or revises one complete proof and independently checks it. |
| `--web-search-mode` | `on` | `on`: enable Survey retrieval only; `off`: disable it. All other roles always have retrieval disabled; there is no per-role enable option. |
| `--web-search-model` | primary model | Model for source-search calls. |
| `--web-search-reasoning-effort` | primary effort | Effort for source-search calls. |
| `--timeout-seconds` | `7200` | Shared timeout in seconds for core, Director, Survey, verifier, and writing calls. |
| `--output` | `outputs` | Root for new problem workspaces. |
| `--json` / `--profile` | off | Machine event output / timing records. |

Each Research or Prover dispatch consumes one proof state, including a partial or mathematically failed attempt. Other roles, fixed Auditor/Kernel checks, and reporting consume calls and time but do not consume this proof-state budget. The cap does not limit total calls, tokens, time, or cost. Only research mode uses the validated difficulty and dependency levels to allocate the budget; an explicit cap can reduce it. Proof mode writes a short brief and uses its saved loop limit, with no research-budget assessment. Survey uses the same configured timeout as other roles. Director calls use low reasoning effort except when the configured effort is `none`. Only Survey may retrieve external sources when enabled. Roles preserve the exact source URLs and locators; retrieved material does not change the literal target or verification authority. Each Codex invocation pins search to live or disabled, rather than inheriting Codex defaults. Switching search off does not make model inference offline. A timed-out model call is stopped without automatic transport replay. After resolving the cause, `resume` retries the saved task with its existing budget and preserved partial artifacts. Operational failures pause before further model-based reporting; optional assessment content errors may use the default budget, while assessment transport failures remain resumable initialization steps.

Mode selection is committed with target extraction before budget allocation. Invalid routing receipts or service failures pause initialization without starting Survey, research or terminal writing. An uncertain but valid recommendation selects research. Batch `pipeline.mode` may be omitted or null; only explicit values emit `--mode`. Omitting batch `pipeline.web_search_mode` defaults to `on`, matching the CLI; set `off` to disable external retrieval.

`resume` accepts execution settings but restores the saved workflow/resource snapshot and budget. See `pace resume --help` for its exact options.

Continued SMI work requires an exact existing question file from the dispatch's
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

`weak` supplies more detailed role prompts. It is a resource bundle name, not an automatic measurement of a model's ability. Both variants use one [shared method library](../src/resources/skills/README.md), including the same mathematical checks and examples. Each run snapshots its selected prompts and the shared Skills under the turn's `resources/` directory. Both prompt variants and the full library ship in the wheel; resume keeps the saved snapshot.

Director and general research roles receive a compact Skill index and read the relevant entry and supporting material for their task. SMI loads methods for its specific phase; statement-only probes do not receive the general index or investigation history. Manuscript roles load the writing guides, while Auditor/Kernel retain independent verification prompts. Skills guide the mathematical work and do not grant a PASS.

## Authentication and custom providers

The simplest setup uses an already authenticated `codex` executable. The batch template has `authentication_mode: "existing_codex_login"` and empty API fields. `provider` does not configure an endpoint by itself.

PACE isolates each role call from personal Codex context: it reuses authentication while ignoring user configuration, disabling global memories and memory generation, personal plugins, Apps, hooks, automatic subagents, AGENTS.md discovery and standalone user Skills. PACE's own saved prompts and Skills remain available. Settings apply only to the child process; no personal configuration files are edited. Use a recent Codex CLI supporting `codex exec --ignore-user-config`; `pace doctor` checks this locally. Custom endpoints must be supplied explicitly through the batch configuration below, rather than inherited from a personal Codex provider profile.

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
A valid receipt remains usable and retains the tool-host warning. SMI checks
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

Ctrl-C stops the owned model/tool processes, preserves partial output and marks the call as interrupted; the CLI exits with code `130`. Parallel workers in that run are cancelled together; completed workers and reserved proof states are retained for `resume`. Other runs continue independently. Operational failures exit with code `2`. A normally completed research run exits with code `0`, even when the mathematical target remains unresolved. `--profile` records timing in the current turn's event log; a timing-write failure does not replace the task's result or interruption. Use `resume` in the same problem directory to continue saved work.

## Writing tasks

Each completed proof/research turn automatically runs one saved sequence: Chinese outline, Chinese report with independent review and necessary revision, English paper outline, English paper with independent review and necessary revision, and PDF build. Transport or initialization failures do not start this sequence. Missing PDF tools pause delivery while preserving reviewed source. `resume` continues the saved stage without rerunning completed research, writing or review. Formatting errors repair the saved draft rather than restarting full authorship. Up to two revisions are attempted per invocation before a resumable pause.

The core also exposes `outline`, `report`, `paper-outline` and `paper` over saved research evidence for new explicit versions. Report and paper branches are independent. Each request saves the complete writing requirements, resource snapshot and selected outline version. `resume` continues the selected writing task; writing status and PDF build status are separate from the mathematical `result`.

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
configured Skill, Survey uses other scholarly and web sources. `off` disables
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

XeLaTeX builds the source, BibTeX resolves an actual bibliography, and `pdffonts` checks for Type 3 fonts. Chinese prose uses ctex and installed Fandol fonts. `pace doctor --pdf` checks the installed toolchain without model inference. Compilation and layout checks do not establish mathematical correctness.

## Recovery and task scheduling

PACE isolates per-call Codex configuration while reusing authentication. Personal instructions, memories, plugins and Skills are excluded from automatic discovery. Packaged PACE resources are supplied explicitly. Partial outputs and original stderr are retained, and input mutations are restored before acceptance. Retry settings are described above.

Research, proof and writing have their own saved tasks. Runtime events contain factual identities, status and output references. The Web application reads these records to create user updates and reports; a presentation failure does not change the mathematical verdict or repeat completed work.

SMI checks the existing call/time budget before every new subphase and reserves at most 64 distinct new phase slots per dispatch. Recovery reuses committed phases. The Host records CREATE or REVISE and assigned output paths for seed work. Continuing an existing question enters Explorer directly; a changed mathematical statement requires an explicit new question.

SMI receipts carry only routing fields and artifact references. Review/change Markdown holds mathematical content. One receipt correction can fix missing controls without creating missing mathematics. An Explorer HANDOFF preserves useful work and its first unresolved gap for Director scheduling; it does not refute the candidate.

Scope-review reuse compares the actual question, target, review rules, recorded dependencies and objections. New or changed relevant evidence triggers a fresh review. Reuse is search context and does not authorize a final proof verdict.

Host-assigned mechanism identities and deduplication task identities are bound at receipt acceptance. Coverage selects promoted mechanisms, and deduplication returns the assigned tasks with valid predecessor references. A completed task saves its artifact and checkpoint before success is published. Ordinary recovery preserves that commit when later bookkeeping or external presentation is interrupted.
