"""Select and freeze task inputs; no model calls or mathematical judgments."""
from __future__ import annotations

import json
from pathlib import Path

from pace.contracts.research.memory import binding, check_binding, check_source_ownership, digest, public_file, read_json
from pace.runtime.output.workspace import _write_json_atomic, work_ref
from pace.runtime.output.results import relocate_links


def load_index(root):
    path = Path(root) / 'memory/research_index.json'
    return json.loads(path.read_text()) if path.is_file() else {'obligations': {}, 'applications': {}, 'attempts': []}


def director_archive_refs(root: Path) -> tuple[str, ...]:
    """Authorize the sources named by Host navigation, without making them required reading.

    Only known Host fields grant access. Links in mathematical manuscripts do not
    expand permissions, and specialists and blind probes do not inherit this view.
    """
    refs = []
    dossier_path = root / 'memory/active_problem_dossier.json'
    if dossier_path.is_file():
        dossier = json.loads(dossier_path.read_text())
        refs.extend(dossier.get(k, '') for k in (
            'problem_ref', 'target_contract_ref', 'orientation_ref', 'problem_brief_ref'))
    index = load_index(root)
    for obligation in index['obligations'].values():
        refs.extend((obligation['statement_ref'], obligation['scope_ref']))
    for route in index.get('routes', {}).values():
        refs.extend((route['ref'], route.get('relations_ref', '')))
    for attempt in index['attempts']:
        refs.extend((attempt['change_ref'], attempt['result_ref'],
                     attempt['attempt'].get('obstacle_ref', ''),
                     attempt['attempt'].get('reopen_condition_ref', '')))
    for application in index['applications'].values():
        refs.extend((application['argument_ref'], *application['premise_refs'],
                     application.get('review_ref', '')))
    refs = tuple(dict.fromkeys(ref for ref in refs if ref))
    for ref in refs:
        public_file(root, ref)
    return refs


def validate_context(root: Path, context_ref: str) -> dict:
    context = read_json(root, context_ref)
    if context.get('schema_version') != 'research_context/v1': raise ValueError('unsupported research context')
    for row in context['bindings']: check_binding(root, row)
    check_binding(root, context['route_context'])
    frozen = read_json(root, context['route_context']['ref'])
    check_binding(root, frozen['target'])
    for obligation in frozen['obligations']:
        for row in obligation['bindings']: check_binding(root, row)
    return context


def freeze_context(root: Path, assignment_ref: str, decision, input_refs) -> dict:
    folder = Path(assignment_ref).parent
    context_ref = str(folder / 'context.json')
    if (root / context_ref).exists():
        context = validate_context(root, context_ref)
        if context['decision_hash'] != digest(decision.to_json()): raise ValueError('frozen decision differs from assignment')
        return context
    index = load_index(root)
    check_binding(root, index['target'])
    ids = list(decision.obligation_ids) or ['target']
    if any(i not in index['obligations'] for i in ids): raise ValueError('unknown task obligation')
    selected = list(input_refs)
    selected += list(decision.change_refs)
    related = set(ids)
    pending = list(ids)
    while pending:
        for parent in index['obligations'][pending.pop()].get('parent_ids', []):
            if parent not in related: related.add(parent); pending.append(parent)
    for ident in ids:
        obligation = index['obligations'][ident]
        for row in obligation['bindings']: check_binding(root, row)
        selected += [obligation['statement_ref'], obligation['scope_ref']]
        for parent in obligation.get('parent_ids', []):
            for row in index['obligations'][parent]['bindings']: check_binding(root, row)
            selected += [index['obligations'][parent]['statement_ref']]
        for group in obligation.get('premise_groups', []):
            for premise in group:
                for row in index['obligations'][premise]['bindings']: check_binding(root, row)
                selected += [index['obligations'][premise]['statement_ref'], index['obligations'][premise]['scope_ref']]
        for app in index.get('applications', {}).values():
            if app['obligation_id'] == ident:
                selected += [app['argument_ref'], *app['premise_refs']]
                if app.get('review_ref'): selected.append(app['review_ref'])
    attempts = [a for a in index.get('attempts', []) if related & set(a['obligation_ids'])]
    # Retain scoped obstacles even when old; the latest attempt provides the continuation.
    for attempt in attempts:
        if attempt['attempt']['obstacle_ref']: selected.append(attempt['attempt']['obstacle_ref'])
    if attempts: selected.append(attempts[-1]['change_ref'])
    route_path = root / work_ref(root, 'state/current_route.json')
    route = json.loads(route_path.read_text()) if route_path.is_file() else {}
    if route:
        if route.get('sha256'): check_binding(root, route)
        if route.get('relations_sha256'):
            check_binding(root, {'ref': route['relations_ref'], 'sha256': route['relations_sha256']})
        selected += [route['ref']]
        if route.get('relations_ref'): selected.append(route['relations_ref'])
    # Navigation is a frozen entry, not a reason to expand the complete archive.
    archives = list(decision.archive_refs)
    required, bindings, aliases = [], [], {}
    for original in dict.fromkeys((*selected, *archives)):
        source = public_file(root, original)
        check_source_ownership(root, binding(root, original))
        mutable = original.startswith(('memory/', 'results/')) or '/state/' in original
        ref = original
        if mutable:
            destination = root / folder / 'snapshot' / original
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == '.md':
                destination.write_text(relocate_links(source.read_text(), source, destination))
            else: destination.write_bytes(source.read_bytes())
            ref = destination.relative_to(root).as_posix()
        row = binding(root, ref)
        row.update(source_ref=original, source_sha256=digest(source.read_bytes()))
        bindings.append(row); aliases[original] = ref
        if original in selected: required.append(ref)
    route_ref = str(folder / 'route_context.json')
    frozen_route = {**route, 'ref': aliases.get(route.get('ref'), route.get('ref', ''))}
    route_context = {'schema_version': 'route_context/v1', 'route': frozen_route,
                     'target': binding(root, work_ref(root, 'input/target_contract.json')),
                     'branch_id': decision.branch_id or 'b-target',
                     'obligations': [index['obligations'][i] for i in ids],
                     'related_attempt_refs': [a['change_ref'] for a in attempts]}
    _write_json_atomic(root / route_ref, route_context)
    context = {'schema_version': 'research_context/v1', 'assignment_ref': assignment_ref,
               'decision_hash': digest(decision.to_json()), 'required_refs': list(dict.fromkeys(required)),
               'archive_refs': [aliases[r] for r in archives if aliases[r] not in required],
               'bindings': bindings, 'route_context': binding(root, route_ref),
               'branch_id': route_context['branch_id'], 'obligation_ids': ids,
               'selected_dependencies': list(dict.fromkeys(selected)),
               'access_policy': 'TASK_DEPENDENCIES_WITH_ON_DEMAND_ARCHIVE',
               'required_bytes': sum((root/r).stat().st_size for r in dict.fromkeys(required))}
    _write_json_atomic(root / context_ref, context)
    return context


def task_context(root: Path, output_ref: str) -> dict:
    ref = str(Path(output_ref).parent / 'assignment/context.json')
    return validate_context(root, ref) if (root / ref).is_file() else {}


def route_base(root: Path, output_ref: str) -> str:
    context = task_context(root, output_ref)
    if context:
        return read_json(root, context['route_context']['ref'])['route'].get('ref', '')
    path = root / work_ref(root, 'state/current_route.json')
    return json.loads(path.read_text())['ref'] if path.is_file() else ''
