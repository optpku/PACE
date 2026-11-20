"""Observe one Chat Completions HTTP attempt without changing its payload."""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import partial


class RoleDeadline(TimeoutError):
    """The logical model call has no remaining time."""


def remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RoleDeadline('logical model-call time limit reached; conversation saved for resume')
    return remaining


class _ProbeDeadline(TimeoutError):
    pass


class _ObservedConnection(http.client.HTTPConnection):
    def __init__(self, *args, attempt, **kwargs):
        self.attempt = attempt
        super().__init__(*args, **kwargs)

    def connect(self):
        self.attempt.phase = 'connect'
        self.timeout = min(30, self.attempt.remaining() or 30)
        super().connect()
        self.attempt.phase = 'send_request'
        if self.sock is None:
            raise ConnectionError("connection opened without a socket")
        self.sock.settimeout(self.attempt.remaining())

    def getresponse(self):
        self.attempt.phase = 'wait_first_byte'
        if self.sock is not None:
            self.sock.settimeout(self.attempt.remaining())
            self.attempt.socket = self.sock
        return super().getresponse()


class _HTTPSConnection(_ObservedConnection, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, attempt):
        super().__init__()
        self.attempt = attempt

    def http_open(self, req):
        return self.do_open(partial(_ObservedConnection, attempt=self.attempt), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, attempt):
        super().__init__()
        self.attempt = attempt

    def https_open(self, req):
        return self.do_open(partial(_HTTPSConnection, attempt=self.attempt), req)


@dataclass
class HTTPAttempt:
    role_deadline: float | None
    probe_deadline: float | None = None
    phase: str = 'connect'
    status: int | None = None
    request_id: str = ''
    socket: socket.socket | None = None

    def remaining(self) -> float | None:
        remaining = remaining_seconds(self.role_deadline)
        if self.probe_deadline is None:
            return remaining
        probe_remaining = self.probe_deadline - time.monotonic()
        if probe_remaining <= 0:
            raise _ProbeDeadline('short recovery probe timed out')
        return min(remaining, probe_remaining) if remaining is not None else probe_remaining

    def _headers(self, status, headers, key):
        self.status = status
        for name in ('x-request-id', 'request-id', 'x-correlation-id'):
            value = headers.get(name) if headers else None
            if value:
                self.request_id = re.sub(r'[\r\n]', '', value.replace(key, '[REDACTED]'))[:256]
                break

    def _read(self, response, limit=None):
        data = bytearray()
        while limit is None or len(data) < limit:
            length = getattr(response, 'length', None)
            if length == 0:
                break
            timeout = self.remaining()
            if self.socket is not None:
                self.socket.settimeout(timeout)
            size = 64 * 1024 if limit is None else min(64 * 1024, limit - len(data))
            chunk = response.read1(size)
            if not chunk:
                if length:
                    raise http.client.IncompleteRead(bytes(data), length)
                break
            data.extend(chunk)
        self.remaining()
        return data

    def run(self, request, key):
        opener = urllib.request.build_opener(_HTTPHandler(self), _HTTPSHandler(self))
        try:
            response = opener.open(request, timeout=self.remaining())
        except urllib.error.HTTPError as exc:
            with exc:
                self._headers(exc.code, exc.headers, key)
                self.phase = 'http_status'
                transient = exc.code in {408, 429, 500, 502, 503, 504}
                body = ''
                if not transient:
                    try:
                        body = self._read(exc.fp, limit=8192).decode('utf-8', errors='replace')
                    except (AttributeError, OSError, http.client.HTTPException):
                        body = 'Error response body could not be read.'
                    body = re.sub(r'(?i)Bearer\s+[^\s"\\]+', 'Bearer [REDACTED]',
                                  body.replace(key, '[REDACTED]'))
            raise HTTPAttemptError(f'Chat Completions returned HTTP {exc.code}', transient, body) from None
        with response:
            self._headers(response.status, response.headers, key)
            self.phase = 'read_response'
            data = self._read(response)
        self.phase = 'decode_response'
        return json.loads(data)


class HTTPAttemptError(RuntimeError):
    def __init__(self, message: str, transient: bool, body: str = ''):
        super().__init__(message)
        self.transient = transient
        self.body = body


def network_failure(exc) -> tuple[str, bool]:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    while isinstance(reason, urllib.error.URLError):
        reason = reason.reason
    # Configuration and trust failures do not heal by sending the same request.
    transient = isinstance(reason, (
        ConnectionError, TimeoutError, http.client.IncompleteRead, ssl.SSLEOFError,
    )) or (isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN)
    if isinstance(reason, OSError) and reason.errno is not None:
        from errno import EHOSTUNREACH, ENETDOWN, ENETRESET, ENETUNREACH, ETIMEDOUT
        transient |= reason.errno in {EHOSTUNREACH, ENETDOWN, ENETRESET, ENETUNREACH, ETIMEDOUT}
    return type(reason).__name__, transient
