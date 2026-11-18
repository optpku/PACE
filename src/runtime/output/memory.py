"""Persist agent-authored notes and index their original evidence references."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from pace.runtime.context_state import empty_search_progress
from .results import FINDINGS_ARCHIVE_REF, FINDINGS_INDEX_REF, relocate_links
from .workspace import RunWorkspace, stage_directories, work_ref

logger = logging.getLogger(__name__)


def note_ref(result_ref: str, *, verification: bool = False) -> str:
    path = Path(result_ref)
    suffix = "-verification" if verification else ""
    return f"memory/notes/{path.parts[1]}-{path.parent.name}{suffix}.md"


def initialize_memory(workspace: RunWorkspace, *, context_manifest: dict) -> None:
    """Retain scoped evidence across turns and bind the current progress view."""

    path = workspace.path("memory/verified_progress.json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    progress = empty_search_progress(
        target_contract_id=context_manifest["target_contract_id"],
        target_contract_ref=context_manifest["target_contract_ref"],
    )
    for key in ("verified_relations", "rejected_relations"):
        progress[key] = previous.get(key, [])
    workspace.write_json("memory/active_problem_dossier.json", context_manifest)
    workspace.write_json("memory/verified_progress.json", progress)
    workspace.path("memory/notes").mkdir(exist_ok=True)
    refresh_memory_index(workspace.root)


def commit_memory_notes(
    root: Path, *, result_refs: Iterable[str],
    verification_values: Iterable[Mapping] = (),
) -> tuple[str, ...]:
    """Only the parent publishes committed task or bound Kernel drafts once."""

    workspace = RunWorkspace(root=root, problem_id=root.name)
    published = []
    for result_ref in dict.fromkeys(result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        result = json.loads((root / result_ref).read_text(encoding="utf-8"))
        if result["status"] != "SUCCEEDED":
            continue
        directory = Path(result_ref).parent
        target_ref = f"work/{directory.parts[1]}/input/target_contract.json"
        ref = _publish_note(workspace, source_ref=f"{directory}/memory_note.md",
                            destination_ref=note_ref(result_ref),
                            evidence_refs=(target_ref, result["deliverable_ref"], result_ref))
        if ref:
            published.append(ref)
    for value in verification_values:
        progress_ref = value.get("verified_progress_ref")
        if not progress_ref:
            continue
        local = json.loads((root / progress_ref).read_text(encoding="utf-8"))
        directory = Path(progress_ref).parent
        result_ref = f"{directory.parent}/agent_result.json"
        ref = _publish_note(workspace, source_ref=f"{directory}/memory_note.md",
                            destination_ref=note_ref(result_ref, verification=True),
                            evidence_refs=(local["target_contract_ref"], local["last_proof_ref"],
                                           local["last_verification_ref"], value["verification_binding_ref"]))
        if ref:
            published.append(ref)
    if published:
        try:
            refresh_memory_index(root)
        except OSError:
            logger.warning("Memory notes committed; navigation refresh failed", exc_info=True)
    return tuple(dict.fromkeys(published))


def _publish_note(workspace: RunWorkspace, *, source_ref: str, destination_ref: str,
                  evidence_refs: tuple[str, ...]) -> str:
    source = workspace.path(source_ref)
    if not source.is_file():
        return ""
    destination = workspace.path(destination_ref)
    if destination.is_file():
        return destination_ref
    try:
        body = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Cannot read optional memory draft: %s", source_ref, exc_info=True)
        return ""
    if not body.strip():
        return ""
    body = relocate_links(body, source, destination)
    lines = [body.rstrip(), "", "---", "", "用途：SEARCH_CONTEXT_ONLY。范围及依据由以下原始文件保留："]
    for ref in dict.fromkeys(ref for ref in (source_ref, *evidence_refs) if ref):
        link = Path(os.path.relpath(workspace.path(ref), destination.parent)).as_posix()
        lines.append(f"- [{ref}]({link})")
    try:
        workspace.write_text_atomic(destination_ref, "\n".join(lines) + "\n")
    except OSError:
        logger.warning("Cannot publish optional memory draft: %s", source_ref, exc_info=True)
        return ""
    return destination_ref


def refresh_memory_index(root: Path) -> None:
    """Generate navigation without summarizing or judging mathematics."""

    workspace = RunWorkspace(root=root, problem_id=root.name)
    lines = ["# 研究记忆", "", "记忆条目用于寻找研究上下文。复用时读取原始目标、假设、论证与核验记录；本轮验收以当前 literal target 为准。", "",
             "## 当前上下文", ""]
    for label, ref in (("当前问题", work_ref(root, "input/problem.md")),
                       ("当前目标", work_ref(root, "input/target_contract.json")),
                       ("输入索引", "memory/active_problem_dossier.json"),
                       ("累计核验关系与当前阻塞", "memory/verified_progress.json")):
        link = Path(os.path.relpath(root / ref, root / "memory")).as_posix()
        lines.append(f"- [{label}]({link})")
    lines.extend(["", "既往核验关系保留原始目标引用；跨轮使用需要重新检查范围与应用条件。"])
    from pace.contracts.research.memory import enabled
    index_path = root / RESEARCH_INDEX_REF
    if enabled(root) and index_path.is_file():
        index = json.loads(index_path.read_text())
        latest_plan = index.get('current_route', {}).get('ref', '')
        lines.extend(['', '## 当前义务与应用', '',
                      '以下为绑定核验记录的索引；CLOSED 不代替原题的最终独立验收。', '',
                      '| 义务 | 范围 | 状态 | 原始陈述 |', '|---|---|---|---|'])
        for row in index['obligations'].values():
            label = str(row['id']).replace('|', '\\|').replace('\n', ' ')
            link = Path(os.path.relpath(root / row['statement_ref'], root / 'memory')).as_posix()
            link += row.get('statement_locator', '')
            lines.append(f"| {label} | {row['kind']} | {row['status']} | [原稿]({link}) |")
        lines.extend(['', '[精确版本、应用审查、尝试与成本记录](research_index.json)',
                      f"待核验应用：{sum(a['status'] == 'PROPOSED' for a in index['applications'].values())}。"])
        progress = [str(Path(a['result_ref']).parent / 'progress.md') for a in index['attempts'][-6:]]
        if progress:
            lines.extend(['', '## 最近任务进展', '', 'Reporter 整理的进展仅供导航；核验状态以原始审查为准。', ''])
            for ref in progress:
                if (root / ref).is_file():
                    link = Path(os.path.relpath(root / ref, root / 'memory')).as_posix()
                    lines.append(f'- [{Path(ref).parent.name}]({link})')
    else:
        latest_plan = ""
        for turn in sorted((root / "work").glob("turn*")):
            for directory in stage_directories(root, turn_id=turn.name):
                path = directory / "agent_result.json"
                if path.is_file():
                    result = json.loads(path.read_text(encoding="utf-8"))
                    if result["agent_id"] == "planner" and result["status"] == "SUCCEEDED":
                        latest_plan = result["deliverable_ref"]
    if latest_plan:
        link = Path(os.path.relpath(root / latest_plan, root / "memory")).as_posix()
        lines.extend(["", f"最近 Planner 交付物（适用范围见原稿）：[{latest_plan}]({link})"])
    lines.extend(["", "## 记忆条目", ""])
    notes = sorted((root / "memory/notes").glob("*.md"))
    lines.extend(f"- [{p.stem}](notes/{p.name})" for p in notes)
    if not notes:
        lines.append("尚无已提交的研究记忆。")
    lines.extend(["", "## 成果入口", ""])
    for label, ref in (("当前成果总览", FINDINGS_INDEX_REF), ("核验原稿档案", FINDINGS_ARCHIVE_REF)):
        if (root / ref).is_file():
            link = Path(os.path.relpath(root / ref, root / "memory")).as_posix()
            lines.append(f"- [{label}]({link})")
    workspace.write_text_atomic("memory/README.md", "\n".join(lines) + "\n")


# Research memory is a derived, SEARCH_CONTEXT_ONLY view of committed artifacts.
RESEARCH_INDEX_REF = 'memory/research_index.json'


def _empty_research_index(root: Path) -> dict:
    from pace.contracts.research.memory import binding, digest
    target_ref = work_ref(root, 'input/target_contract.json')
    target = binding(root, target_ref)
    return {'schema_version': 'research_index/v1', 'authority': 'SEARCH_CONTEXT_ONLY',
            'target': target, 'commits': {}, 'applications': {}, 'attempts': [],
            'obligation_history': {}, 'routes': {}, 'costs': {},
            'obligations': {'target': {'id': 'target', 'kind': 'TARGET_REQUIRED',
                'version': digest(target), 'statement_ref': target_ref, 'scope_ref': target_ref,
                'bindings': [target], 'parent_ids': [], 'premise_groups': [],
                'branch_id': 'b-target', 'status': 'OPEN'}}}


def initialize_research_memory(root: Path) -> None:
    from pace.contracts.research.memory import enabled
    if not enabled(root): return
    path = root / RESEARCH_INDEX_REF
    fresh = _empty_research_index(root)
    if path.is_file():
        previous = json.loads(path.read_text())
        if previous['target'] == fresh['target']: return
        if previous['target']['ref'] == fresh['target']['ref']:
            raise ValueError('research target changed within the same turn; restore its original version')
        # Cross-turn records remain in their owners; only the new target gets a current view.
    from .workspace import _write_json_atomic
    _write_json_atomic(path, fresh)


def validate_research_delivery(root: Path, result: dict, assignment_ref: str, *, author_only=False) -> dict:
    from pace.contracts.research.memory import enabled, reporter_owned, prepare_delta, prepare_route
    from pace.runtime.research_context import load_index, validate_context
    if not enabled(root): return {}
    folder = Path(assignment_ref).parent.parent
    context_ref = str(folder / 'assignment/context.json')
    if not (root / context_ref).is_file(): return {}  # proof-mode authors have a different fixed chain
    context = validate_context(root, context_ref)
    index = load_index(root)
    obligations = dict(index['obligations'])
    # Validate against the dispatched versions, even if a parallel task changes the current route.
    from pace.contracts.research.memory import read_json
    frozen = read_json(root, context['route_context']['ref'])
    obligations.update({row['id']: row for row in frozen['obligations']})
    payload = {}
    if result['agent_id'] == 'planner':
        ref = str(folder / 'route_relations.json')
        payload['route'] = prepare_route(root, ref, obligations)
        payload['route_ref'] = ref
    if reporter_owned(root):
        if author_only or result['agent_id'] not in {'research', 'prover', 'smi'}:
            return payload
        from pace.reporting.research_progress import restore_reporter_delivery
        restore_reporter_delivery(root, str(folder / 'agent_result.json'))
        payload['reporter_ref'] = str(folder / 'reporter_result.json')
    delta_ref = str(folder / 'research_delta.json')
    if (root / delta_ref).is_file() or result['agent_id'] in {'research', 'prover', 'smi'}:
        payload['delta'] = prepare_delta(root, delta_ref, obligations)
        delta = payload['delta']
        if delta['branch_id'] != context['branch_id'] or not set(delta['obligation_ids']) <= set(context['obligation_ids']):
            raise ValueError('research delta must retain the assigned branch and obligation identities')
        payload['delta_ref'] = delta_ref
    return payload


def commit_research_memory(root: Path, result_refs=None) -> None:
    """Run only in the parent after its task state is durable; replay is idempotent."""
    from pace.contracts.research.memory import binding, check_binding, digest, enabled, read_json
    from .workspace import _write_json_atomic
    from .events import append_event
    if not enabled(root): return
    initialize_research_memory(root)
    committed_refs = _committed_research_results(root)
    committed = set(committed_refs)
    if result_refs is None: result_refs = committed_refs
    index_path = root / RESEARCH_INDEX_REF
    index = json.loads(index_path.read_text())
    from .events import read_events
    notified = {e['idempotency_key'] for e in read_events(root) if e['event_kind'] == 'RESEARCH_MEMORY_COMMITTED'}
    for ident, saved_ref in index['commits'].items():
        if ident not in notified:
            saved = read_json(root, saved_ref)
            append_event(root, 'RESEARCH_MEMORY_COMMITTED', idempotency_key=ident, commit_ref=saved_ref,
                         result_ref=saved['result_ref'], branch_id=saved['branch_id'])
    known_refs = set(index['commits'].values())
    for ref in dict.fromkeys(result_refs):
        commit_ref = str(Path(ref).parent / 'memory_commit.json')
        if ref not in committed or Path(ref).name != 'agent_result.json': continue
        folder = Path(ref).parent
        if (root / folder / 'reporter_result.json').is_file() and any(
                not (root / folder / name).is_file() for name in ('progress.md', 'research_delta.json')):
            from pace.reporting.research_progress import restore_reporter_delivery
            restore_reporter_delivery(root, ref)
        if commit_ref in known_refs: continue
        result = read_json(root, ref)
        if result['status'] != 'SUCCEEDED': continue
        folder = Path(ref).parent
        commit_ref = str(folder / 'memory_commit.json')
        if (root / commit_ref).is_file():
            record = read_json(root, commit_ref)
            for row in record['bindings']: check_binding(root, row)
        else:
            assignment_ref = str(folder / 'assignment/task.json')
            context_ref = str(folder / 'assignment/context.json')
            if not (root / context_ref).is_file(): continue
            context = read_json(root, context_ref)
            payload = validate_research_delivery(root, result, assignment_ref)
            refs = [ref, result['deliverable_ref'], assignment_ref, context_ref,
                    *(payload[k] for k in ('route_ref', 'delta_ref', 'reporter_ref') if k in payload)]
            record = {'schema_version': 'research_memory_commit/v1', 'result_ref': ref,
                      'target': index['target'], 'payload': payload,
                      'bindings': [binding(root, r) for r in refs],
                      'branch_id': context['branch_id'], 'obligation_ids': context['obligation_ids']}
            record['commit_id'] = digest(record)
            _write_json_atomic(root / commit_ref, record)
        if record['commit_id'] not in index['commits']:
            _apply_research_commit(index, record, commit_ref)
            _write_json_atomic(index_path, index)
        append_event(root, 'RESEARCH_MEMORY_COMMITTED', idempotency_key=record['commit_id'],
                     commit_ref=commit_ref, result_ref=ref, branch_id=record['branch_id'])

    route = index.get('current_route')
    if route:
        path = root / work_ref(root, 'state/current_route.json')
        if not path.is_file() or json.loads(path.read_text()) != route:
            _write_json_atomic(path, route)
    refresh_application_memory(root)


def _apply_research_commit(index, record, commit_ref):
    from pace.contracts.research.memory import application_review_identity, digest
    payload = record['payload']
    if 'route' in payload:
        for row in payload['route']['obligations']:
            old = index['obligations'].get(row['id'])
            if old and old['version'] != row['version']:
                index['obligation_history'][old['id'] + ':' + old['version']] = old
                for app in index['applications'].values():
                    if app['obligation_id'] == row['id']: app['status'] = 'INVALIDATED'
            elif old and row.get('disposition', 'OPEN') == 'OPEN' and old['status'] == 'CLOSED':
                row = {**row, 'status': old['status']}
            index['obligations'][row['id']] = row
        source = record['bindings'][1]
        relation = next(b for b in record['bindings'] if b['ref'] == payload['route_ref'])
        index['routes'][record['branch_id']] = {'ref': source['ref'], 'sha256': source['sha256'],
            'relations_ref': relation['ref'], 'relations_sha256': relation['sha256'],
            'committed_result_ref': record['result_ref']}
        index['current_route'] = index['routes'][record['branch_id']]
    if 'delta' in payload:
        delta = payload['delta']
        index['attempts'].append({**delta, 'commit_ref': commit_ref, 'result_ref': record['result_ref']})
        applications = index['applications']
        legacy_keys = {application_review_identity(app): key for key, app in applications.items()
                       if 'proposal_ids' not in app}
        for app in delta['applications']:
            entry = {**app, 'result_ref': record['result_ref'], 'target': record['target']}
            if 'proposal_ids' in app:
                identity = application_review_identity(entry)
                key = legacy_keys.get(identity, identity)
            else:
                key = digest({'target': record['target'], 'application': app})
            applications.setdefault(key, {**entry, 'application_key': key})
    index['commits'][record['commit_id']] = commit_ref


def rebuild_research_index(root: Path) -> dict:
    """Rebuild derived memory from parent-committed results, not author status labels."""
    from pace.contracts.research.memory import check_binding, read_json
    from .workspace import _write_json_atomic
    index = _empty_research_index(root)
    for ref in _committed_research_results(root):
        if Path(ref).name != 'agent_result.json': continue
        commit_ref = str(Path(ref).parent / 'memory_commit.json')
        if not (root / commit_ref).is_file(): continue
        record = read_json(root, commit_ref)
        if record['target'] != index['target']: continue
        for row in record['bindings']: check_binding(root, row)
        if record['commit_id'] not in index['commits']: _apply_research_commit(index, record, commit_ref)
    _write_json_atomic(root / RESEARCH_INDEX_REF, index)
    # Application receipts are an independent, precisely bound source of authority.
    refresh_application_memory(root)
    update_research_costs(root)
    return read_json(root, RESEARCH_INDEX_REF)


def _committed_research_results(root: Path) -> list[str]:
    """Both engines keep mathematical authority in their own durable task state."""
    path = root / work_ref(root, 'state/director_state.json')
    if path.is_file():
        return json.loads(path.read_text())['completed_result_refs']
    path = root / work_ref(root, 'state/proof_state.json')
    if not path.is_file(): return []
    completed = json.loads(path.read_text())['completed_refs']
    results = []
    for source in completed:
        ref = str(Path(source).parent / 'agent_result.json')
        if (root / ref).is_file():
            result = json.loads((root / ref).read_text())
            if result.get('status') == 'SUCCEEDED' and result.get('deliverable_ref') == source:
                results.append(ref)
    return list(dict.fromkeys(results))


def whole_proof_application_routes(root: Path, index: dict, *, state: dict | None = None) -> dict[str, str]:
    """Identify the candidate's own root application, pending or checked as a whole."""
    from pace.contracts.research.memory import read_json

    if state is None:
        ref = work_ref(root, 'state/director_state.json')
        state = read_json(root, ref) if (root / ref).is_file() else {}
    phase = state.get('phase')
    if phase != 'VERIFY' and not (phase == 'FINALIZE' and state.get('terminal_result') == 'PASS'):
        return {}
    candidate = state.get('candidate_ref')
    owner = state.get('last_specialist_result_ref')
    if not candidate or not owner or owner not in state.get('completed_result_refs', []):
        return {}
    target = index['obligations']['target']
    keys = [key for key, app in index['applications'].items()
            if app['result_ref'] == owner and app['argument_ref'] == candidate
            and app['obligation_id'] == 'target' and app['obligation_version'] == target['version']
            and app['target'] == index['target']]
    if not keys:
        return {}
    if phase == 'VERIFY':
        return dict.fromkeys(keys, '')

    from pace.contracts.proof.binding import resolve_verification

    checkpoint = read_json(root, state['last_verification_ref'])
    if checkpoint['specialist_result_ref'] != owner or checkpoint['round_index'] != state['round_index']:
        raise ValueError('whole proof verification belongs to a different handoff')
    receipt = checkpoint['verification']
    resolved = resolve_verification(root, receipt, target_contract_id=state['target_contract_id'],
                                    candidate_ref=candidate)
    if resolved['status'] != 'PASS' or not resolved['binding'].get('verification_inputs_ref'):
        return {}
    if resolved['binding']['target_contract_ref'] != index['target']['ref']:
        return {}
    return dict.fromkeys(keys, receipt['verification_binding_ref'])


def refresh_application_memory(root: Path) -> None:
    from pace.contracts.research.memory import application_review_bindings, check_binding, read_json, enabled
    from .workspace import _write_json_atomic
    if not enabled(root): return
    index = read_json(root, RESEARCH_INDEX_REF)
    whole_routes = whole_proof_application_routes(root, index)
    for key, app in index['applications'].items():
        if app.get('status') == 'REVIEWED_AS_WHOLE':
            app['status'] = 'PROPOSED'
            app.pop('whole_verification_ref', None)
        obligation = index['obligations'][app['obligation_id']]
        if obligation['version'] != app['obligation_version']:
            app['status'] = 'INVALIDATED'; continue
        expected = application_review_bindings(app)
        try:
            for row in expected:
                check_binding(root, row)
        except (ValueError, OSError):
            app['status'] = 'INVALIDATED'; obligation['status'] = 'OPEN'; continue
        if whole_ref := whole_routes.get(key):
            app.update(status='REVIEWED_AS_WHOLE', whole_verification_ref=whole_ref)
            continue
        ref = str(Path(app['result_ref']).parent / 'verification' / f"application-{app['application_key']}.json")
        if not (root / ref).is_file(): continue
        record = read_json(root, ref)
        if record.get('application_key') != app['application_key']: raise ValueError('application review binding mismatch')
        if record['bindings'] != expected: raise ValueError('application review has different dependencies')
        try:
            check_binding(root, record['request_binding'])
            if record.get('review_binding'): check_binding(root, record['review_binding'])
        except (ValueError, OSError):
            app['status'] = 'INVALIDATED'; continue
        verdict = record['verdict']
        checks = verdict.get('application_checks', [])
        valid = (verdict.get('stage_status') == 'PASS' and verdict.get('reuse_authorized') is True
                 and len(checks) == 1 and checks[0].get('obligation_id') == app['obligation_id']
                 and checks[0].get('obligation_version') == app['obligation_version']
                 and checks[0].get('status') == 'APPLICABLE' and record.get('review_binding')
                 and checks[0].get('review_ref') == record['review_binding']['ref'])
        app.update(status='APPLICABLE' if valid else 'NOT_ESTABLISHED', review_ref=ref)
        if valid: obligation['status'] = 'CLOSED'
    # A rejected/invalidated application cannot erase another valid application.
    for obligation in index['obligations'].values():
        disposition = obligation.get('disposition', 'OPEN')
        obligation['status'] = disposition if disposition != 'OPEN' else ('CLOSED' if any(
            a['status'] == 'APPLICABLE' and a['obligation_id'] == obligation['id']
            and a['obligation_version'] == obligation['version'] for a in index['applications'].values()) else 'OPEN')
    _write_json_atomic(root / RESEARCH_INDEX_REF, index)


def research_brief(root: Path) -> str:
    """Compact selection prompt; detailed manuscripts remain referenced on demand."""
    from pace.contracts.research.memory import enabled, read_json
    if not enabled(root): return ''
    index = read_json(root, RESEARCH_INDEX_REF)
    counts = {}
    for attempt in index['attempts']:
        related = set(attempt['obligation_ids'])
        pending = list(related)
        while pending:
            for parent in index['obligations'].get(pending.pop(), {}).get('parent_ids', []):
                if parent not in related: related.add(parent); pending.append(parent)
        for ident in related:
            counts[ident] = counts.get(ident, 0) + 1
            if any(app['status'] == 'APPLICABLE' and app['obligation_id'] == ident
                   and app['result_ref'] == attempt['result_ref'] for app in index['applications'].values()):
                counts[ident] = 0
    return '\nResearch memory (SEARCH_CONTEXT_ONLY; read bound evidence before reuse):\n' + json.dumps({
        'obligations': [{k: o[k] for k in ('id', 'version', 'kind', 'branch_id', 'status', 'statement_ref', 'statement_locator', 'parent_ids') if k in o}
                        for o in index['obligations'].values()],
        'pending_applications': [k for k, a in index['applications'].items() if a['status'] == 'PROPOSED'],
        'recent_attempts': [{k: a[k] for k in ('obligation_ids', 'obligation_versions', 'branch_id', 'change_ref', 'attempt', 'result_ref')}
                            for a in index['attempts'][-6:]],
        'reassessment_due': [k for k, v in counts.items() if v >= 3],
        'note': 'Reassessment is advisory, not a stop. Normal node completion does not require replanning. Cite a concrete change for repeated attempts.',
        'archive_ref': RESEARCH_INDEX_REF,
        'branch_costs': index.get('costs', {}).get('branches', {}),
    }, ensure_ascii=False)


def update_research_costs(root: Path) -> None:
    """Consume each timing event once; role totals do not double-count nested spans."""
    from pace.contracts.research.memory import enabled, read_json
    from .events import read_events
    from .workspace import _write_json_atomic
    if not enabled(root) or not (root / RESEARCH_INDEX_REF).is_file(): return
    index = read_json(root, RESEARCH_INDEX_REF)
    cursor = index.get('cost_cursor', 0)
    costs = index.setdefault('costs', {})
    calls = costs.setdefault('calls', {})
    for event in read_events(root):
        if event['sequence'] <= cursor: continue
        if event['event_kind'] == 'SEMANTIC_CALL_STARTED' and event.get('call_ref'):
            record = json.loads((root / event['call_ref']).read_text())
            calls.setdefault(event['call_id'], {'branch_id': record.get('branch_id', ''),
                'obligation_ids': record.get('obligation_ids', []), 'role': record['role'],
                'elapsed_seconds': None, 'transport_attempts': 0})
        elif event['event_kind'] == 'TRANSPORT_TIMING':
            key = event.get('call_id') or event.get('metadata', {}).get('call_id')
            if key in calls:
                row = calls[key]
                row['elapsed_seconds'] = (row['elapsed_seconds'] or 0) + event['elapsed_seconds']
                row['transport_attempts'] += 1
        index['cost_cursor'] = event['sequence']
    branches = {}
    for call in calls.values():
        row = branches.setdefault(call['branch_id'] or 'unassigned', {'logical_calls': 0,
            'cumulative_call_seconds': 0, 'missing_timing_calls': 0, 'transport_attempts': 0})
        row['logical_calls'] += 1
        row['cumulative_call_seconds'] += call['elapsed_seconds'] or 0
        row['missing_timing_calls'] += call['elapsed_seconds'] is None
        row['transport_attempts'] += call['transport_attempts']
    costs['branches'] = branches
    sessions = root / work_ref(root, 'state/sessions.json')
    if sessions.is_file():
        rows = json.loads(sessions.read_text())
        costs['completed_session_seconds'] = sum(r['elapsed_seconds'] for r in rows if r.get('ended_at'))
        costs['session_measurement_complete'] = all(r.get('ended_at') for r in rows)
    _write_json_atomic(root / RESEARCH_INDEX_REF, index)
