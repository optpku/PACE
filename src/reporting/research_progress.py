"""One resumable, non-authoritative progress receipt per mathematical handoff."""
from __future__ import annotations

import json
from pathlib import Path

from pace.contracts.research.memory import (
    OUTCOMES, binding, check_binding, digest, prepare_delta_payload, read_json, reporter_owned,
)
from pace.runtime.output.workspace import _write_json_atomic, _write_text_atomic, work_ref
from pace.runtime.research_context import task_context
from pace.runtime.schema import closed_object
from pace.runtime.semantic import (
    SemanticCallSpec, SemanticRepresentationFailure, StructuredSemanticResult,
    semantic_call, validate_semantic_payload,
)


def reporter_schema(request=None):
    text = {'type': 'string'}
    refs = {'type': 'array', 'items': text}
    def reference(allowed, *, optional=False):
        field = {'type': 'string', 'description':
                 'One exact workspace-relative file path. No section numbers, Markdown, commentary or multiple paths.'}
        if request is not None and (allowed or optional):
            field['enum'] = list(dict.fromkeys([*allowed, *([''] if optional else [])]))
        return field
    originals = (request or {}).get('original_refs', [])
    dependencies = [row['ref'] for row in (request or {}).get('bindings', [])]
    application = closed_object({'id': text, 'obligation_id': text,
        'argument_ref': reference([r for r in originals if Path(r).suffix == '.md']),
        'premise_refs': {'type': 'array', 'items': reference(dependencies)}})
    delta = closed_object({
        'schema_version': {'type': 'string', 'enum': ['research_delta/v1']},
        'obligation_ids': refs, 'branch_id': text, 'change_ref': reference(originals),
        'applications': {'type': 'array', 'items': application},
        'attempt': closed_object({'outcome': {'type': 'string', 'enum': sorted(OUTCOMES)},
                                 'obstacle_ref': reference(originals, optional=True),
                                 'reopen_condition_ref': reference(originals, optional=True)}),
    })
    if request is not None and not any(Path(r).suffix == '.md' for r in originals):
        delta['properties']['applications']['maxItems'] = 0
    return closed_object({'summary': text, 'delta': delta})


def _saved_response(root, folder):
    """Reuse a delivered receipt even if validation or the process stopped afterwards."""
    from pace.runtime.output.events import read_events
    latest = None
    for event in reversed(read_events(root)):
        if event.get('event_kind') != 'SEMANTIC_CALL_STARTED' or event.get('role') != 'RESEARCH_REPORTER':
            continue
        record_ref = event.get('call_ref', '')
        if not record_ref or Path(record_ref).parent.parent != folder / 'calls':
            continue
        record = json.loads((root / record_ref).read_text())
        # Transport records may not yet have their semantic response_ref committed.
        for response in sorted((root / record_ref).parent.glob('attempts/*/response.txt'), reverse=True):
            try:
                payload = json.loads(response.read_text())
            except (ValueError, OSError):
                continue
            if latest is None:
                latest = (response.relative_to(root).as_posix(), payload)
            if record.get('stage') == f'research_reporter_{folder.name}':
                return (*latest, payload)
            break
    return (*latest, latest[1]) if latest else None


def _without_references(payload):
    """Receipt repair can change references, not the reported mathematics or verdict."""
    if isinstance(payload, dict):
        return {k: _without_references(v) for k, v in payload.items()
                if k not in {'change_ref', 'argument_ref', 'premise_refs', 'obstacle_ref', 'reopen_condition_ref'}}
    if isinstance(payload, list):
        return [_without_references(v) for v in payload]
    return payload


def restore_reporter_delivery(root: Path, result_ref: str) -> dict:
    """The accepted receipt owns projections, including a crash between their writes."""
    folder = Path(result_ref).parent
    saved = read_json(root, str(folder / 'reporter_result.json'))
    if saved['schema_version'] != 'research_reporter/v1' or saved['result_ref'] != result_ref:
        raise ValueError('Reporter receipt belongs to a different handoff')
    if saved['receipt_id'] != digest({k: v for k, v in saved.items() if k != 'receipt_id'}):
        raise ValueError('Reporter receipt changed after acceptance')
    for row in saved['bindings']:
        check_binding(root, row)
    _write_json_atomic(root / folder / 'research_delta.json', saved['payload']['delta'])
    _write_text_atomic(root / folder / 'progress.md', '# Research progress\n\n' + saved['payload']['summary'].strip() + '\n')
    return saved


def run_research_reporter(*, llm, workdir: Path, result_ref: str) -> dict | None:
    root = Path(workdir)
    if not reporter_owned(root):
        return None
    result = read_json(root, result_ref)
    if result.get('status') != 'SUCCEEDED' or result['agent_id'] not in {'research', 'prover', 'smi'}:
        return None
    folder = Path(result_ref).parent
    receipt_ref = str(folder / 'reporter_result.json')
    if (root / receipt_ref).is_file():
        return restore_reporter_delivery(root, result_ref)
    context = task_context(root, result['deliverable_ref'])
    if not context:
        raise ValueError('Reporter requires the saved task context; do not reconstruct it from the current route')
    request_ref = str(folder / 'reporter_request.json')
    if not (root / request_ref).is_file():
        # Read this handoff and its direct dependencies, not the complete historical archive.
        originals = [result['deliverable_ref'], *result.get('published_refs', ())]
        originals = list(dict.fromkeys(r for r in originals if (root / r).is_file()
                                      and Path(r).is_relative_to(folder)))
        required = originals
        if result['agent_id'] == 'smi':
            required = [r for r in originals if r == result['deliverable_ref']
                        or Path(r).name in {'change.md', 'outcome.json'} or 'findings' in Path(r).parts]
        refs = list(dict.fromkeys([result_ref, *required, *context['required_refs'], context['route_context']['ref']]))
        archives = list(dict.fromkeys([*(r for r in originals if r not in refs), *context['archive_refs']]))
        _write_json_atomic(root / request_ref, {
            'schema_version': 'research_reporter_request/v1', 'result_ref': result_ref,
            'execution_mode': read_json(root, work_ref(root, 'state/research_execution_policy.json')).get('mode', 'research'),
            'obligation_ids': context['obligation_ids'], 'branch_id': context['branch_id'],
            'original_refs': originals, 'required_refs': refs, 'archive_refs': archives,
            'bindings': [binding(root, r) for r in dict.fromkeys((*refs, *archives))],
            'context': binding(root, str(folder / 'assignment/context.json')),
        })
    request = read_json(root, request_ref)
    for row in [request['context'], *request['bindings']]:
        check_binding(root, row)
    frozen = read_json(root, context['route_context']['ref'])
    obligations = {row['id']: row for row in frozen['obligations']}
    schema = reporter_schema(request)
    original_payload = None

    def validate(payload):
        # Report every invalid reference in one correction, rather than stopping
        # at the first failing filesystem lookup. Enums use the frozen manifest.
        errors = validate_semantic_payload(payload, schema)
        if original_payload:
            expected = _without_references(original_payload)
            if isinstance(expected, dict) and isinstance(expected.get('delta'), dict):
                expected['delta'].update(branch_id=request['branch_id'], obligation_ids=request['obligation_ids'])
                if request.get('execution_mode') == 'proof':
                    expected['delta']['applications'] = []
            if _without_references(payload) != expected:
                errors.append({'reason_code': 'reporter_repair_changed_content', 'json_pointer': '/',
                    'message': 'Restore the original summary, outcome and application identities; repair references only.'})
        if errors:
            return errors
        try:
            if not payload['summary'].strip():
                raise ValueError('a short progress summary is required')
            if request.get('execution_mode') == 'proof' and payload['delta']['applications']:
                raise ValueError('proof mode reports its candidate only: applications must be empty; the existing whole-proof verifier decides acceptance')
            delta = prepare_delta_payload(root, payload['delta'], obligations, local_result_ref=result_ref)
            if delta['branch_id'] != request['branch_id'] or delta['obligation_ids'] != request['obligation_ids']:
                raise ValueError('retain the assigned branch and obligation identities')
            if delta['change_ref'] not in request['original_refs']:
                raise ValueError('change_ref must identify an original mathematical handoff')
            # Reporter must not supply an application proof in its own summary or a control file.
            for app in delta['applications']:
                if app['argument_ref'] not in request['original_refs'] or Path(app['argument_ref']).suffix != '.md':
                    raise ValueError('application argument must already exist in the mathematical handoff')
            for row in [request['context'], *request['bindings']]:
                check_binding(root, row)
            return []
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return [{'reason_code': 'reporter_receipt_invalid', 'json_pointer': '/delta', 'message': str(exc)}]

    def accept(payload):
        # Commit before event publication. A replay only restores these exact projections.
        delta = payload['delta']
        references = [delta['change_ref'], delta['attempt']['obstacle_ref'], delta['attempt']['reopen_condition_ref']]
        for app in delta['applications']:
            references += [app['argument_ref'], *app['premise_refs']]
        bindings = [binding(root, request_ref), request['context'], *request['bindings']]
        bindings += [binding(root, ref) for ref in dict.fromkeys(references) if ref]
        saved = {'schema_version': 'research_reporter/v1', 'result_ref': result_ref,
                 'authority': 'SEARCH_CONTEXT_ONLY', 'payload': payload, 'bindings': bindings}
        saved['receipt_id'] = digest(saved)
        _write_json_atomic(root / receipt_ref, saved)

    from pace.runtime.prompts import load_prompt_template
    prompt = load_prompt_template('output/research_progress.md', workdir=root)
    prompt += '\nFrozen Reporter request: `' + request_ref + '`\n'
    prompt += '\nReference fields must be exact paths from their schema enums, without sections or commentary.\n'
    prompt += '\nRequired originals and direct dependencies:\n' + '\n'.join(f'- `{ref}`' for ref in request['required_refs'])
    prompt += '\nOn-demand archive (not required reading):\n' + '\n'.join(f'- `{ref}`' for ref in request['archive_refs'])
    spec = SemanticCallSpec(role='RESEARCH_REPORTER', semantic_schema=schema,
            host_schema_version='research_reporter/v1', authority='SEARCH_CONTEXT_ONLY', fatal_scope='CALL',
            validator=validate, on_accept=accept)
    metadata = {'task_id': folder.name + '-reporter', 'stage_ref': str(folder),
                'archive_refs': request['archive_refs'], 'output_refs': [], 'output_dirs': [],
                'receipt_correction_owner': True}
    saved = _saved_response(root, folder)
    if saved:
        raw_ref, payload, original_payload = saved
        diagnostics = validate(payload)
        if not diagnostics:
            accept(payload)
            return restore_reporter_delivery(root, result_ref)
    else:
        reply = semantic_call(llm, prompt, workdir=root, call_name=f'research_reporter_{folder.name}',
            spec=spec, input_refs=(request_ref, request['context']['ref'], *request['required_refs']),
            host_metadata=metadata)
        if isinstance(reply, StructuredSemanticResult):
            return restore_reporter_delivery(root, result_ref)
        if not isinstance(reply, SemanticRepresentationFailure) or any(
                row.get('stage') == 'acceptance' for row in reply.diagnostics):
            raise RuntimeError('Reporter interrupted; resume without repeating the mathematical author: '
                               + json.dumps(list(reply.diagnostics), ensure_ascii=False))
        raw_ref, payload, diagnostics = reply.host['response_ref'], reply.payload, list(reply.diagnostics)
        original_payload = payload

    # A path correction is a control task. Supply the previous receipt, all
    # diagnostics and exact allowed paths in the schema, not the entire library.
    correction = (
        '## JSON receipt correction\n'
        f'Repair the saved Reporter receipt in `{raw_ref}`. This is a reference/serialization repair only. '
        'Return the complete receipt with every invalid reference fixed in one pass. '
        'For each reference choose ONE exact path from that field\'s enum; never append sections, '
        'backticks or explanations. Annotations belong in prose, not a path field. '
        'Preserve the ORIGINAL summary, outcome and application identities supplied below exactly. '
        f'Retain the assigned branch {request["branch_id"]!r} and obligation IDs {request["obligation_ids"]!r}. '
        + ('Proof mode requires applications=[]; remove any mistakenly proposed application. '
           if request.get('execution_mode') == 'proof' else '') +
        'Do not independently review the mathematics, reread source papers, browse directories, '
        'invent a premise, remove an application, or rewrite manuscripts. '
        'The previous receipt and schema contain the information for this correction.\n'
        + json.dumps(diagnostics, ensure_ascii=False)
        + '\nOriginal content to preserve (except the invalid file references):\n'
        + json.dumps(original_payload, ensure_ascii=False))
    reply = semantic_call(llm, correction, workdir=root,
        call_name=f'research_reporter_{folder.name}_receipt_correction',
        spec=spec, input_refs=(raw_ref, request_ref),
        host_metadata={**metadata, 'archive_refs': [], 'read_scope': 'declared',
                       'receipt_corrections': 1})
    if not isinstance(reply, StructuredSemanticResult):
        raise RuntimeError('Reporter interrupted; resume this handoff without repeating the mathematical author: '
                           + json.dumps(list(reply.diagnostics), ensure_ascii=False))
    return restore_reporter_delivery(root, result_ref)
