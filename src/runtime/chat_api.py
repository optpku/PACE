"""Chat Completions transport with workspace-scoped file tools."""
from __future__ import annotations

import json
import hashlib
from contextvars import ContextVar
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from pace.runtime.llm import CodexLLM, TransportSuccess, TransportFailure, RETRY_NON_TRANSIENT, RETRY_TRANSIENT
from pace.runtime.output.calls import attempt_directory
from pace.runtime.output.events import append_event, record_transport_attempt_started, record_transport_attempt_finished
from pace.runtime.output.workspace import _write_json_atomic, _write_text_atomic, is_call_path, path_has_symlink_component, work_ref
from pace.runtime.output.sources import fetch_source, read_source, search_source

_request_context = ContextVar('pace_chat_request_context', default=None)


def _same_navigation_snapshot(root, ref, path, saved_text):
    """Permit bookkeeping changes while keeping the task and evidence fixed."""
    if ref == work_ref(root, 'state/director_state.json'):
        bookkeeping = {
            'phase', 'round_index', 'pending_assignment_ref', 'pending_dispatch_ref',
            'pending_prompt_ref', 'last_directive_ref', 'last_composer_manifest_ref',
        }
    elif ref == 'memory/research_index.json':
        bookkeeping = {'costs', 'cost_cursor'}
    else:
        return False
    if not isinstance(saved_text, str):
        return False
    try:
        previous = json.loads(saved_text)
        current = json.loads(path.read_bytes())
    except (UnicodeError, ValueError):
        return False
    return (isinstance(previous, dict) and isinstance(current, dict)
            and {key: value for key, value in previous.items() if key not in bookkeeping}
            == {key: value for key, value in current.items() if key not in bookkeeping})


def _resume_messages(root, directory, context, capabilities, initial):
    """Continue an interrupted conversation in this exact task, never another role's."""
    from pace.runtime.output.calls import call_directory
    from pace.runtime.output.events import read_events
    calls = call_directory(root, context['call_ref']).parent
    for event in reversed(read_events(root)):
        ref = event.get('call_ref', '')
        if event.get('event_kind') != 'SEMANTIC_CALL_STARTED' or not ref or ref == context['call_ref']:
            continue
        record_path = root / ref
        if record_path.parent.parent != calls or event.get('role') != context.get('role'):
            continue
        record = json.loads(record_path.read_text())
        if record.get('status') not in {'STARTED', 'INTERFACE_FAILED', 'ABORTED'}:
            continue
        for chat in sorted(record_path.parent.glob('attempts/*/chat.json'), reverse=True):
            old_caps = json.loads(chat.with_name('capabilities.json').read_text())
            keys = ('provider', 'model', 'reasoning_effort_requested', 'tools', 'required_refs', 'archive_refs')
            if any(old_caps.get(k) != capabilities.get(k) for k in keys):
                continue
            # New checkpoints also bind the provider and complete tool contract.
            if old_caps.get('request_contract') not in (None, capabilities['request_contract']):
                continue
            # A historical structured schema cannot be reconstructed safely.
            if 'request_contract' not in old_caps and 'submit_result' in old_caps['tools']:
                continue
            messages = json.loads(chat.read_text())
            if messages[:2] != initial:
                continue
            tools = WorkspaceTools(root, context, web_search='fetch_url' in capabilities['tools'])
            functions = {}
            writes = {}
            reads = []
            for message in messages[2:]:
                for call in message.get('tool_calls') or []:
                    functions[call['id']] = call['function']
                if message['role'] != 'tool':
                    continue
                value = json.loads(message['content'])
                function = functions.get(message.get('tool_call_id'), {})
                if value.get('error') or not function:
                    continue
                args = json.loads(function['arguments'])
                if function['name'] in {'read_file', 'search_file'}:
                    if not tools.readable(args['path']):
                        raise ValueError('saved conversation refers to a file outside the current input view')
                    reads.append((args['path'], value.get('source_sha256'), value.get('content')))
                elif function['name'] == 'fetch_url' and value.get('artifact_ref'):
                    original_ref = value['artifact_ref']
                    reads.append((original_ref, value.get('sha256') or value.get('source_sha256'), None))
                    if value.get('read_ref'):
                        reads.append((value['read_ref'], value.get('source_sha256'), None))
                elif function['name'] == 'write_file':
                    writes[args['path']] = args['content']
            for ref, digest, saved_text in reads:
                # Drafts may have been revised by this same conversation. Their
                # final write is checked below; immutable inputs must still match.
                if ref in writes:
                    continue
                path = tools.path(ref)
                if (digest != hashlib.sha256(path.read_bytes()).hexdigest()
                        and not _same_navigation_snapshot(root, ref, path, saved_text)):
                    raise ValueError(f'saved conversation input changed: {ref}')
            for ref, content in writes.items():
                if tools.path(ref).read_text() != content:
                    raise ValueError(f'saved conversation output changed: {ref}')
            append_event(root, 'CHAT_CONVERSATION_RESUMED', call_id=context['call_id'],
                         prior_call_id=record['call_id'], chat_ref=chat.relative_to(root).as_posix(),
                         completed_tool_calls=sum(m['role'] == 'tool' for m in messages))
            _write_json_atomic(directory / 'chat.json', messages)
            return messages
    return initial


def _tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


FILE_TOOLS = [
    _tool('read_file', 'Read a content-aware text/PDF view. HTML CSS, fonts and binary assets are omitted. '
          'Use view=embedded for page data after search_file, not raw HTML. Coordinates apply to the selected '
          'view, not raw HTML. Lines/pages are one-based; characters are zero-based, end exclusive. '
          'Replies default to 16000 characters; follow next_read to continue, including within a long line. '
          'max_chars changes response page size, never file acceptance.',
          {'path': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'},
           'start_page': {'type': 'integer'}, 'end_page': {'type': 'integer'},
           'start_char': {'type': 'integer'}, 'end_char': {'type': 'integer'}, 'max_chars': {'type': 'integer'},
           'view': {'type': 'string', 'enum':['text','embedded']}}, ['path']),
    _tool('search_file', 'Find literal keywords in a readable file without returning whole lines. '
          'Use view=embedded on an HTML source to locate dynamic-page data or script URLs; '
          'CSS/fonts are excluded. Results have character offsets and read_args; follow next_search '
          'for more matches. path must be a FILE, never a directory: use list_files first '
          'and search selected files. Defaults: 20 matches, 200 surrounding characters per side.',
          {'path': {'type': 'string'}, 'query': {'type': 'string'},
           'view': {'type': 'string', 'enum':['text','embedded']}, 'start_char': {'type': 'integer'},
           'max_results': {'type': 'integer'}, 'context_chars': {'type': 'integer'},
           'case_sensitive': {'type': 'boolean'}}, ['path','query']),
    _tool('write_file', 'Write a complete UTF-8 artifact to an assigned output path. Inputs are read-only.',
          {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
    _tool('list_files', 'List readable workspace files below a directory; no outside paths.',
          {'path': {'type': 'string'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['path']),
]
FETCH_TOOL = _tool('fetch_url',
    'Survey only: download and cache a complete public HTTP(S) document. Return extracted HTML/PDF/text '
    'with source references. Defaults: first 200 lines, response pages of 16000 characters. '
    'Follow next_read for the rest. full_text=true selects all text, still paginated by max_chars. '
    'For a dynamic page use search_file on artifact_ref with view=embedded; do not read whole programs. '
    'HTML scripts are not executed. Repeated URL reads reuse the saved version.',
    {'url': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'},
     'start_page': {'type': 'integer'}, 'end_page': {'type': 'integer'}, 'full_text': {'type': 'boolean'},
     'start_char': {'type': 'integer'}, 'end_char': {'type': 'integer'}, 'max_chars': {'type': 'integer'},
     'view': {'type': 'string', 'enum':['text','embedded']}}, ['url'])


class WorkspaceTools:
    def __init__(self, root, context, *, web_search=False, timeout_seconds=None):
        self.root = Path(root).resolve()
        self.inputs = set(filter(None, context.get('input_refs', ())))
        self.outputs = set(filter(None, context.get('output_refs', ())))
        self.output_dirs = tuple(filter(None, context.get('output_dirs', ())))
        self.role = str(context.get('role', '')).upper()
        self.declared_only = context.get('read_scope') == 'declared'
        self.blind = self.role in {
            'SMI_BLIND_PROVER_PROBE', 'SMI_REFUTE_PROVER_PROBE', 'SMI_FRONTIER_REFUTE_PROBE'}
        self.archives = set() if self.blind else set(filter(None, context.get('archive_refs', ())))
        if not self.blind and not self.declared_only:
            # Public finding links must reach their exact manuscripts and reviews,
            # including evidence committed in an earlier turn. No directory grant.
            from pace.runtime.output.results import finding_entries
            for row in finding_entries(self.root):
                self.archives.update(row[key] for key in ('source_ref', 'verification_ref'))
        from pace.contracts.research.memory import public_file
        for ref in self.archives:
            public_file(self.root, ref)
        self.web = web_search and self.role == 'SURVEY'
        self.timeout_seconds = timeout_seconds
        sources = next((p for p in self.output_dirs if Path(p).name == 'sources'), work_ref(self.root, 'sources'))
        self.source_cache = self.root / sources / 'web_cache'
        state = self.root / work_ref(self.root, 'state/director_state.json')
        self.committed = tuple(str(Path(r).parent) for r in json.loads(state.read_text()).get('completed_result_refs', [])
                               if Path(r).name == 'agent_result.json') if state.is_file() else ()

    def path(self, ref):
        if (not isinstance(ref, str) or not ref or '\\' in ref or Path(ref).is_absolute()
            or PureWindowsPath(ref).drive or (ref != '.' and any(p in {'', '.', '..'} for p in ref.split('/')))):
            raise ValueError('use a workspace-relative path')
        path = self.root / ref
        if path_has_symlink_component(self.root, path) or not path.resolve().is_relative_to(self.root):
            raise ValueError('path escapes the workspace or contains a symlink')
        return path

    def readable(self, ref):
        if ref in self.inputs or ref in self.outputs: return True
        if self.blind: return False
        if ref in self.archives: return True
        if self.declared_only: return False
        path = Path(ref)
        if self.web and (self.root / path).is_relative_to(self.source_cache): return True
        if is_call_path(path) or any(p.startswith(('.', 'private_')) for p in path.parts): return False
        if ref.startswith(('work/runtime/', 'work/state/')) or path.name in {'AGENTS.md', 'config.local.json'}: return False
        if ref.startswith(('memory/', 'results/')): return True
        if '/resources/' in ref:
            return self.role in {'GLOBAL_RESEARCH_DIRECTOR', 'GLOBAL_DIRECTOR', 'DIRECTOR'} or ('/dispatch-' not in ref and '/director-dispatch/' not in ref)
        if any(path.is_relative_to(Path(d)) for d in (*self.committed, *self.output_dirs)): return True
        # A selected immutable snapshot can refer to another file in the same source bundle.
        return any(path.is_relative_to(Path(p).parent) for p in self.inputs if '/sources/' in p)

    def execute(self, name, args):
        if name == 'fetch_url':
            if not self.web: raise ValueError('only Survey may retrieve sources')
            self.path(self.source_cache.relative_to(self.root).as_posix())
            return fetch_source(self.root, self.source_cache, timeout=self.timeout_seconds, **args)
        ref = args['path']
        path = self.path(ref)
        if name == 'list_files':
            rows = [p.relative_to(self.root).as_posix() for p in path.rglob('*')
                    if p.is_file() and not p.is_symlink() and self.readable(p.relative_to(self.root).as_posix())]
            rows.sort()
            if 'offset' not in args and 'limit' not in args: return {'files': rows}
            offset, limit = args.get('offset', 0), args.get('limit', len(rows))
            if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 1:
                raise ValueError('offset must be nonnegative and limit must be positive')
            end = min(len(rows), offset + limit)
            return {'files': rows[offset:end], 'total_files': len(rows),
                    'next_offset': end if end < len(rows) else None}
        if name in {'read_file', 'search_file'}:
            if path.is_dir():
                raise ValueError('path must name one readable file, not a directory; use list_files '
                                 'to obtain permitted files, then read/search a selected file')
            if not self.readable(ref): raise ValueError('file is outside this role input view')
            handler = read_source if name == 'read_file' else search_source
            if 'source_info' in args: raise ValueError('source metadata belongs to the Host')
            info = None
            receipt_ref = (path.parent / 'source.json').relative_to(self.root).as_posix()
            if self.readable(receipt_ref):
                receipt = self.path(receipt_ref)
                if receipt.is_file():
                    saved = json.loads(receipt.read_text())
                    if saved.get('artifact_ref') == ref: info = saved
            result = handler(path, source_info=info, **{k:v for k,v in args.items() if k != 'path'})
            for key in ('next_read', 'next_search'):
                if result.get(key) is not None: result[key]['path'] = ref
            for match in result.get('matches', ()): match['read_args']['path'] = ref
            return {'path': ref, **result}
        if name == 'write_file':
            if path.is_relative_to(self.source_cache): raise ValueError('downloaded source versions are read-only')
            if ref in self.inputs or ref in self.archives or not (ref in self.outputs or any(Path(ref).is_relative_to(Path(d)) for d in self.output_dirs)):
                raise ValueError('write is outside the assigned artifacts, or would modify an input')
            if is_call_path(Path(ref)) or '/state/' in ref or '/assignment/' in ref: raise ValueError('Host checkpoint is not a model output')
            if not isinstance(args['content'], str): raise ValueError('content must be text')
            _write_text_atomic(path, args['content'])
            return {'path': ref, 'bytes': len(args['content'].encode())}
        raise ValueError('unknown workspace tool')


@dataclass
class ChatCompletionsLLM(CodexLLM):
    """Reuse the existing semantic ledger and failure handling, replacing only transport."""
    base_url: str = ''
    api_key_env: str = 'PACE_API_KEY'
    max_retries: int = 8

    def _request(self, messages, tools):
        key = os.environ.get(self.api_key_env, '')
        if not key: raise ValueError(f'{self.api_key_env} is required')
        payload = {'model': self.model, 'messages': messages, 'tools': tools, 'stream': False}
        if self.reasoning_effort != 'none': payload['reasoning_effort'] = self.reasoning_effort
        request = urllib.request.Request(self.base_url.rstrip('/') + '/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        # Retry only this network request. Already executed tools are not replayed.
        for retry in range(self.max_retries + 1):
            runtime = _request_context.get()
            timeout = self.timeout_seconds
            if runtime and runtime['deadline'] is not None:
                timeout = runtime['deadline'] - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError('logical model-call time limit reached; conversation saved for resume')
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                transient = exc.code in {408, 429, 500, 502, 503, 504}
                if runtime:
                    append_event(runtime['root'], 'CHAT_REQUEST_FAILURE', call_id=runtime['call_id'],
                                 attempt=retry + 1, status=exc.code, transient=transient,
                                 elapsed_seconds=time.monotonic() - started,
                                 will_retry=transient and retry < self.max_retries)
                if retry == self.max_retries or exc.code not in {408, 429, 500, 502, 503, 504}:
                    try:
                        body = exc.read().decode('utf-8', errors='replace').replace(key, '[REDACTED]')
                    except OSError:
                        body = 'Error response body could not be read.'
                    body = re.sub(r'(?i)Bearer\s+[^\s"\\]+', 'Bearer [REDACTED]', body)
                    failure = RuntimeError(f'Chat Completions returned HTTP {exc.code}')
                    failure.provider_error = {'status':exc.code, 'body':body,
                        'request_id':exc.headers.get('x-request-id', '') if exc.headers else '',
                        'attempts': retry + 1, 'transient': transient, 'retry_budget_exhausted': transient}
                    raise failure from None
                exc.close()
            except (urllib.error.URLError, TimeoutError) as exc:
                if runtime:
                    append_event(runtime['root'], 'CHAT_REQUEST_FAILURE', call_id=runtime['call_id'],
                                 attempt=retry + 1, error_type=type(exc).__name__, transient=True,
                                 elapsed_seconds=time.monotonic() - started,
                                 will_retry=retry < self.max_retries)
                if retry == self.max_retries:
                    failure = RuntimeError(f'Chat Completions network request failed after {retry + 1} attempts')
                    failure.provider_error = {'transient': True, 'attempts': retry + 1,
                                              'retry_budget_exhausted': True}
                    raise failure from None
            delay = min(2 ** retry, 8)
            if runtime and runtime['deadline'] is not None:
                delay = min(delay, max(0, runtime['deadline'] - time.monotonic()))
            time.sleep(delay)

    def _complete_transport_owned(self, prompt, *, workdir, call_name, output_schema, ledger_context):
        root = Path(workdir).resolve()
        directory = attempt_directory(root, ledger_context['call_ref'], 1)
        invocation = uuid.uuid4().hex
        tools = WorkspaceTools(root, ledger_context, web_search=self.web_search, timeout_seconds=self.timeout_seconds)
        tool_specs = [t for t in FILE_TOOLS if t['function']['name'] != 'write_file'
                      or tools.outputs or tools.output_dirs] + ([FETCH_TOOL] if tools.web else [])
        system = ('You are executing one PACE mathematical role. Use the file tools to read inputs and write the requested artifacts. '
                  'All paths are relative to the run workspace. Inputs are immutable. Do not invent tool results. '
                  'There is no shell or code execution tool. Network retrieval is unavailable except the Survey fetch_url tool. '
                  'Project method skills are local guidance, not proof authority. ')
        if output_schema:
            tool_specs.append({'type': 'function', 'function': {'name': 'submit_result',
                'description': 'Submit the final structured receipt. Do not write it to a file. Call once, after all artifact tools finish.',
                'parameters': output_schema}})
            system += ('After creating required artifacts, call submit_result alone with your final receipt. '
                       'Do not write receipts or Host state files. Do not add explanatory prose. ')
        else:
            system += 'After creating the required artifacts, return a short acknowledgement.'
        system += ' Writable files: ' + json.dumps(sorted(tools.outputs)) + '. Writable directories: ' + json.dumps(tools.output_dirs) + '.'
        if tools.archives:
            system += ' The Host on-demand archive is available read-only; follow the supplied context indexes only when needed.'
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]
        capabilities = {'provider': 'chat-completions', 'model': self.model,
            'tools': [t['function']['name'] for t in tool_specs], 'reasoning_effort_requested': self.reasoning_effort,
            'required_refs': sorted(tools.inputs), 'archive_refs': sorted(tools.archives),
            'request_max_retries': self.max_retries,
            'request_contract': hashlib.sha256(json.dumps({'base_url': self.base_url,
                'tool_specs': tool_specs, 'read_scope': ledger_context.get('read_scope', '')},
                sort_keys=True).encode()).hexdigest()}
        _write_json_atomic(directory / 'capabilities.json', capabilities)
        record_transport_attempt_started(root, call_id=ledger_context['call_id'], call_name=call_name,
            role=ledger_context.get('role', ''), stage=ledger_context.get('stage', call_name),
            invocation_id=invocation, attempt_index=1, artifact_dir=directory)
        started = time.monotonic()
        from pace.runtime.input_protection import protected_inputs
        protection = protected_inputs(root, (*ledger_context.get('input_refs', ()), *tools.archives,
                                             *ledger_context.get('protected_input_refs', ())), directory)
        protection.arm()
        requests = 0
        token = _request_context.set({'root': root, 'call_id': ledger_context['call_id'],
                                     'deadline': started + self.timeout_seconds if self.timeout_seconds else None})
        try:
            messages = _resume_messages(root, directory, ledger_context, capabilities, messages)
            while True:
                from pace.runtime.sessions import model_failure
                if model_failure(root) is not None: raise RuntimeError('another role recorded a terminal model failure')
                if self.timeout_seconds and time.monotonic() - started > self.timeout_seconds:
                    raise TimeoutError('logical model-call time limit reached')
                # A crash may occur after the assistant response or after any
                # one tool. Consume only tool calls without saved replies.
                last_assistant = next((i for i in range(len(messages) - 1, 1, -1)
                                       if messages[i]['role'] == 'assistant'), None)
                answered = {m.get('tool_call_id') for m in messages[last_assistant + 1:]} if last_assistant else set()
                message = messages[last_assistant] if last_assistant else {}
                pending = [c for c in message.get('tool_calls') or [] if c['id'] not in answered]
                terminal = bool(message) and last_assistant == len(messages) - 1 and not message.get('tool_calls')
                if not pending and not terminal:
                    response = self._request(messages, tool_specs)
                    requests += 1
                    choice = response['choices'][0]
                    message = choice['message']
                    if choice.get('finish_reason') == 'length':
                        raise RuntimeError('model output was truncated')
                    # Retain provider reasoning fields when required by a compatible endpoint.
                    messages.append({k: v for k, v in message.items() if k in {'role', 'content', 'tool_calls', 'reasoning_content'}})
                    _write_json_atomic(directory / 'chat.json', messages)
                    pending = message.get('tool_calls') or []
                calls = message.get('tool_calls') or []
                submission = (calls[0]['function'] if len(calls) == 1
                              and calls[0]['function']['name'] == 'submit_result' and output_schema else None)
                if not calls or submission:
                    text = submission['arguments'] if submission else message.get('content') or ''
                    if not isinstance(text, str) or not text.strip(): raise ValueError('empty model response')
                    _write_text_atomic(directory / 'response.txt', text)
                    protection.check(directory)
                    result = TransportSuccess(text=text, invocation_id=invocation, call_name=call_name,
                        exit_code=0, terminal_event_type='chat.completed', accepted_output_source='chat.submit_result' if submission else 'chat.message',
                        output_schema_valid=None, attempt_count=1, artifact_dir=directory,
                        authority_evidence={'source': 'CHAT_COMPLETIONS', 'model_requests': requests})
                    break
                for call in pending:
                    function = call['function']
                    try:
                        if function['name'] == 'submit_result':
                            raise ValueError('submit_result must be called alone after artifact tools finish')
                        if function['name'] not in {t['function']['name'] for t in tool_specs}:
                            raise ValueError('tool is not available for this invocation')
                        args = json.loads(function['arguments'])
                        value = tools.execute(function['name'], args)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        value = {'error': str(exc)}
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value, ensure_ascii=False)})
                    _write_json_atomic(directory / 'chat.json', messages)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            key = os.environ.get(self.api_key_env, '')
            error = str(exc).replace(key, '[REDACTED]') if key else str(exc)
            _write_text_atomic(directory / 'stderr.txt', error)
            if getattr(exc, 'provider_error', None):
                _write_json_atomic(directory / 'provider_error.json', exc.provider_error)
            result = TransportFailure(invocation_id=invocation, call_name=call_name,
                error_code='CHAT_API_ERROR', retry_class=(RETRY_TRANSIENT
                    if isinstance(exc, TimeoutError) or getattr(exc, 'provider_error', {}).get('transient')
                    else RETRY_NON_TRANSIENT), retryable=False,
                exit_code=None, terminal_event_type='chat.failed', attempt_count=1,
                diagnostics=({'message': error},), artifact_dir=directory)
        finally:
            _request_context.reset(token)
            protection.check(directory)
            append_event(root, 'TRANSPORT_TIMING', call_id=ledger_context['call_id'],
                         elapsed_seconds=time.monotonic() - started, model_requests=requests,
                         metadata={'call_id': ledger_context['call_id'], 'attempt_index': 1})
        record_transport_attempt_finished(root, result, call_id=ledger_context['call_id'],
            role=ledger_context.get('role', ''), stage=ledger_context.get('stage', call_name), attempt_index=1)
        return result
