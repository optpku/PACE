"""Conservative, source-located checks for escaped mathematics in manuscripts.

This is a preflight, not a TeX parser or a mathematical review. It never edits
source text. In particular, prose and code spelling out TeX commands are valid.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class MathFormatIssue:
    code: str
    offset: int
    line: int
    column: int
    token: str

    def as_dict(self) -> dict:
        return asdict(self)


def _blank(text: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in text)


def _visible_source(text: str, *, markdown: bool) -> str:
    """Mask non-rendered material without moving source offsets or line numbers."""
    if markdown:
        lines = text.splitlines(keepends=True)
        fence = None
        math_fence = False
        for index, line in enumerate(lines):
            marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
            if fence:
                closing = (marker and marker[1][0] == fence[0]
                           and len(marker[1]) >= len(fence) and not marker[2].strip())
                if closing or not math_fence:
                    lines[index] = _blank(line)
                if closing:
                    if math_fence:
                        lines[index] = "$$" + lines[index][2:]
                    fence = None
            elif marker:
                fence = marker[1]
                math_fence = marker[2].strip() == "math"
                lines[index] = _blank(line)
                if math_fence:
                    lines[index] = "$$" + lines[index][2:]
            elif re.match(r" {0,3}>", line):
                lines[index] = _blank(line)
            elif line.startswith(("    ", "\t")):
                # Prefer a missed diagnostic over editing an indented code
                # example. Fenced math is handled explicitly above.
                lines[index] = _blank(line)
        text = "".join(lines)
        text = re.sub(r"(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", lambda m: _blank(m[0]), text)
        text = re.sub(r"<!--[\s\S]*?-->", lambda m: _blank(m[0]), text)
    else:
        text = re.sub(r"\\begin\{(verbatim\*?|lstlisting|minted)\}[\s\S]*?\\end\{\1\}",
                      lambda m: _blank(m[0]), text)
        text = re.sub(r"\\verb\*?([^\w\s])(.*?)\1", lambda m: _blank(m[0]), text)
        text = re.sub(r"(?<!\\)%[^\n]*", lambda m: _blank(m[0]), text)
    if markdown:
        for start, end in _math_ranges(text):
            text = text[:start] + re.sub(r"(?<!\\)%[^\n]*", lambda m: _blank(m[0]),
                                        text[start:end]) + text[end:]
    return text


_MATH_TOKEN = re.compile(
    r"\$\$|\$|\\[\[\]()]|"
    r"\\(?:begin|end)\{(?:equation\*?|align\*?|alignat\*?|gather\*?|"
    r"multline\*?|eqnarray\*?|displaymath|math)\}"
)


def _escaped(text: str, offset: int) -> bool:
    start = offset
    while start and text[start - 1] == "\\":
        start -= 1
    return (offset - start) % 2 == 1


def _math_ranges(text: str):
    closing = None
    start = 0
    for match in _MATH_TOKEN.finditer(text):
        if _escaped(text, match.start()):
            continue
        token = match[0]
        if closing is None:
            if token in ("$", "$$", r"\(", r"\[") or token.startswith(r"\begin{"):
                start = match.end()
                closing = {r"\(": r"\)", r"\[": r"\]"}.get(
                    token, token.replace(r"\begin", r"\end"))
        elif token == closing:
            yield start, match.start()
            closing = None


def _mask_math_text(text: str) -> str:
    # A word explicitly typeset as text/an operator is not a lost TeX command.
    chars = list(text)
    for match in re.finditer(r"\\(?:text\w*|operatorname\*?|mathrm|mathsf|mathtt|mbox)\s*\{", text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            if not _escaped(text, end):
                depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        chars[match.start():end] = _blank(text[match.start():end])
    return "".join(chars)


def math_format_issues(text: str, *, markdown: bool = True) -> tuple[MathFormatIssue, ...]:
    """Locate concrete escape defects inside visible mathematical regions."""
    visible = _visible_source(text, markdown=markdown)
    found = [(i, "CONTROL_CHARACTER", c) for i, c in enumerate(visible)
             if ord(c) < 32 and c not in "\t\r\n"]
    # TeX comments apply inside math; in a .tex file they also hide delimiters.
    if not markdown:
        visible = re.sub(r"(?<!\\)%[^\n]*", lambda m: _blank(m[0]), visible)
    for start, end in _math_ranges(visible):
        math = re.sub(r"(?<!\\)%[^\n]*", lambda m: _blank(m[0]), visible[start:end])
        math = _mask_math_text(math)
        for match in re.finditer(r"(?<![\\\w])(qquad|quad)(?!\w)", math):
            # An explicitly used identifier is not a damaged spacing command.
            before, after = math[:match.start()].rstrip(), math[match.end():].lstrip()
            if (not before or not after or before[-1:] in "=<>+-*/^_{"
                    or re.match(r"[=<>+*/^_(]|\\(?:in|notin|leq?|geq?)\b", after)):
                continue
            found.append((start + match.start(), "BARE_SPACING_COMMAND", match[0]))
        # A raw brace starts/ends a TeX group; it is never a delimiter token.
        # Do not guess arbitrary macros or require matching delimiter shapes:
        # e.g. \left( ... \right] and \left. ... \right\} are valid.
        for match in re.finditer(
            r"\\(?:left|right|middle|[bB]ig(?:g)?[lrm]?)(?![A-Za-z])\s*[{}]", math
        ):
            if not _escaped(math, match.start()):
                found.append((start + match.start(), "UNESCAPED_SIZED_DELIMITER", match[0]))
    return tuple(MathFormatIssue(code, offset, text.count("\n", 0, offset) + 1,
                                 offset - text.rfind("\n", 0, offset), token)
                 for offset, code, token in sorted(found))



def require_math_format(text: str, *, markdown: bool = True) -> None:
    issues = math_format_issues(text, markdown=markdown)
    if issues:
        raise ValueError("Math formatting requires correction: " + "; ".join(
            f"{issue.line}:{issue.column} {issue.code}" for issue in issues
        ))


def artifact_format_issues(root: Path, ref: str, *, text: str | None = None) -> list[dict]:
    """Batchable local preflight; does not fetch URLs, check anchors or review math.

    ``ref`` is the logical publication location, also when ``text`` is a repair
    copy. Relative links must be checked against that location, not the staging
    directory holding a corrected copy.
    """
    if text is None:
        text = (root / ref).read_bytes().decode("utf-8")
    markdown = Path(ref).suffix.lower() == ".md"
    issues = [{"file": ref, **issue.as_dict()}
              for issue in math_format_issues(text, markdown=markdown)]
    if not markdown:
        return issues
    visible = _visible_source(text, markdown=True)
    chars = list(visible)
    for start, end in _math_ranges(visible):
        chars[start:end] = _blank(visible[start:end])
    visible = "".join(chars)
    # Ordinary inline/image links and reference definitions. Do not interpret
    # bare paths, code, TeX, or link titles as links.
    links = re.compile(
        r"!?\[[^\]\n]*\]\(\s*(?P<inline><[^>\n]*>|(?:[^\s()]|\([^()\n]*\))+)"
        r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^\n]*?\)))?\s*\)"
        r"|^ {0,3}\[[^\]\n]+\]:[ \t]*(?P<definition><[^>\n]*>|\S+)", re.M)
    for match in links.finditer(visible):
        raw_target = match["inline"] or match["definition"]
        source_token = raw_target.strip("<>")
        target = source_token
        target = re.sub(r"\\([() ])", r"\1", target)
        try:
            url = urlsplit(target)
        except ValueError:
            continue  # malformed external URLs are outside this local check
        if url.scheme or url.netloc or not url.path:
            continue
        path = Path(unquote(url.path))
        path = path if path.is_absolute() else (root / ref).parent / path
        if not path.exists():
            offset = match.start("inline" if match["inline"] else "definition")
            offset += int(raw_target.startswith("<"))
            issues.append({"file": ref, "code": "MISSING_LOCAL_LINK", "offset": offset,
                           "line": text.count("\n", 0, offset) + 1,
                           "column": offset - text.rfind("\n", 0, offset), "token": source_token})
    return issues


def format_repair_plan(root: Path, ref: str, *, text: str | None = None) -> dict:
    """Describe small, permitted repairs; uncertain defects have no automatic edit.

    This is a representation policy, not a proof of mathematical equivalence.
    A missing spacing escape is automatic only between comma/semicolon-separated
    mathematical clauses or numeric expressions. Literal identifiers, text, code and source quotations
    must never be normalized by a whole-file replacement.
    """
    if text is None:
        text = (root / ref).read_bytes().decode("utf-8")
    markdown = Path(ref).suffix.lower() == ".md"
    visible = _visible_source(text, markdown=markdown)
    ranges = list(_math_ranges(visible))
    paragraphs = [(m.start(), m.end()) for m in re.finditer(r"\r?\n[ \t]*\r?\n", text)]
    issues = artifact_format_issues(root, ref, text=text)
    for issue in issues:
        start, token = issue["offset"], issue["token"]
        issue.update(replacements=[], automatic=False)
        if issue["code"] == "BARE_SPACING_COMMAND":
            issue["replacements"] = ["\\" + token]
            region = next(((a, b) for a, b in ranges if a <= start < b), None)
            if region:
                a, b = region
                math = _mask_math_text(visible[a:b])
                left, right = math[:start-a].rstrip(), math[start-a+len(token):].lstrip()
                relation = r"[=<>]|\\(?:leq?|geq?|in|sim|asymp)(?![A-Za-z])"
                # Stop at the next spacing separator, not at commas in subscripts.
                rhs = re.split(r"(?:\\?qquad|\\?quad)\b", right, maxsplit=1)[0]
                lhs = re.split(r"(?:\\?qquad|\\?quad)\b", left)[-1]
                numeric = r"(?:[0-9\s.,+*/^(){}\[\]-]|\\(?:cdot|times|frac|sqrt)(?![A-Za-z]))+"
                numeric_list = (re.fullmatch(numeric, lhs) and re.fullmatch(numeric, rhs)
                                and re.search(r"\d", lhs) and re.search(r"\d", rhs))
                identifier = re.search(
                    r"(?<![\\\w])" + token + r"\s*(?:[=<>_^]|\\(?:in|notin)(?![A-Za-z]))", visible)
                redefined = re.search(
                    r"\\(?:def|let|newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\\"
                    + token + r"(?![A-Za-z])", visible)
                issue["automatic"] = bool(
                    left.endswith((",", ";")) and not identifier and not redefined
                    and ((re.search(relation, left) and re.search(relation, rhs)) or numeric_list)
                    and not re.match(r"[=<>+*/^_(]|\\(?:in|notin)\b", right))
        elif issue["code"] == "UNESCAPED_SIZED_DELIMITER":
            issue.update(replacements=[token[:-1] + "\\" + token[-1]], automatic=True)
        elif issue["code"] == "CONTROL_CHARACTER":
            # Only recover a complete known TeX command, never guess arbitrary
            # control characters, tabs/newlines, or an unknown macro name.
            prefix = {"\b": "b", "\f": "f", "\v": "v", "\a": "a"}.get(token)
            suffix = re.match(r"[A-Za-z]+", text[start+1:])
            commands = {"beta", "big", "bigl", "bigr", "bigg", "biggl", "biggr",
                        "frac", "forall", "varphi", "varepsilon", "vert", "alpha"}
            if prefix and suffix and prefix + suffix[0] in commands and any(
                    a <= start < b for a, b in ranges):
                issue.update(replacements=["\\" + prefix], automatic=True)
        elif issue["code"] == "MISSING_LOCAL_LINK":
            # A run-relative path accidentally used as document-relative has
            # an exact identity. Never search by basename or invent a citation.
            url = urlsplit(token)
            path = Path(unquote(url.path))
            candidate = root / path
            if (not path.is_absolute() and ".." not in path.parts
                    and candidate.is_file() and candidate.resolve().is_relative_to(root.resolve())):
                import os
                from urllib.parse import quote
                replacement = quote(os.path.relpath(candidate, (root / ref).parent), safe="/._-~")
                if url.query:
                    replacement += "?" + url.query
                if url.fragment:
                    replacement += "#" + url.fragment
                issue.update(replacements=[replacement], automatic=True)
        # Give the model complete affected math blocks/paragraphs, not a byte
        # prefix of the manuscript. The full source remains available on demand.
        lo = max((b for a, b in paragraphs if b <= start), default=0)
        hi = next((a for a, b in paragraphs if a >= start + len(token)), len(text))
        for a, b in ranges:
            if a <= start < b:
                lo = min(lo, text.rfind("\n", 0, a) + 1)
                end = text.find("\n", b)
                hi = max(hi, len(text) if end < 0 else end)
                break
        issue["context_start"], issue["context_end"] = lo, hi
    spans = []
    for issue in sorted(issues, key=lambda row: row["context_start"]):
        lo, hi = issue["context_start"], issue["context_end"]
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return {"version": 1, "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "logical_ref": ref, "issues": issues,
            "excerpts": [{"offset": a, "text": text[a:b]} for a, b in spans]}


def local_format_repair(text: str, plan: dict) -> str:
    """Apply only the plan's unambiguous edits, preserving every other byte."""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != plan["source_sha256"]:
        raise ValueError("Format plan belongs to a different source version")
    for issue in sorted(plan["issues"], key=lambda row: row["offset"], reverse=True):
        if issue["automatic"]:
            start, token = issue["offset"], issue["token"]
            text = text[:start] + issue["replacements"][0] + text[start+len(token):]
    return text


def validate_format_revision(original: str, updated: str, plan: dict, *, root: Path | None = None) -> None:
    """Accept only declared token substitutions, not edits to adjacent mathematics."""
    if hashlib.sha256(original.encode("utf-8")).hexdigest() != plan["source_sha256"]:
        raise ValueError("Format plan belongs to a different source version")
    pieces, links, end = [], {}, 0
    for index, issue in enumerate(sorted(plan["issues"], key=lambda row: row["offset"])):
        start, token = issue["offset"], issue["token"]
        if start < end or original[start:start+len(token)] != token:
            raise ValueError("Format repair locations overlap or changed")
        pieces.append(re.escape(original[end:start]))
        if issue["code"] == "MISSING_LOCAL_LINK" and not issue["replacements"]:
            # Models may resolve a flagged link, never replace the label or
            # neighbouring proof. Existence is not proof of source applicability;
            # the ordinary independent review still checks the evidence.
            key = f"link_{index}"
            pieces.append(f"(?P<{key}>[^\\r\\n<>]+?)")
            links[key] = token
        else:
            pieces.append("(?:" + "|".join(re.escape(v) for v in
                          [token, *issue["replacements"]]) + ")")
        end = start + len(token)
    pieces.append(re.escape(original[end:]))
    match = re.fullmatch("".join(pieces), updated)
    if not match:
        raise ValueError("Format patch changes unflagged text or an unsupported token; requires content revision")
    for key, token in links.items():
        target = match[key]
        if target == token:
            continue
        if root is None:
            raise ValueError("Link repair requires the run-local evidence root")
        url = urlsplit(target)
        path = Path(unquote(url.path))
        resolved = ((root / plan["logical_ref"]).parent / path).resolve()
        if (url.scheme or url.netloc or path.is_absolute() or not url.path
                or not resolved.is_relative_to(root.resolve()) or not resolved.exists()
                or any(c in target for c in "[]()\"'`")):
            raise ValueError("Format link patch must reference existing run-local evidence")
    if updated == original:
        raise ValueError("Format patch made no correction; unresolved defect requires content revision")
