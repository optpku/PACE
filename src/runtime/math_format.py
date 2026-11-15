"""Conservative, source-located checks for escaped mathematics in manuscripts.

This is a preflight, not a TeX parser or a mathematical review. It never edits
source text. In particular, prose and code spelling out TeX commands are valid.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


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
        text = "".join(lines)
        text = re.sub(r"(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", lambda m: _blank(m[0]), text)
        text = re.sub(r"<!--[\s\S]*?-->", lambda m: _blank(m[0]), text)
    else:
        text = re.sub(r"\\begin\{(verbatim\*?|lstlisting|minted)\}[\s\S]*?\\end\{\1\}",
                      lambda m: _blank(m[0]), text)
        text = re.sub(r"\\verb\*?([^\w\s])(.*?)\1", lambda m: _blank(m[0]), text)
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
    """Locate bare spacing commands in math and non-printing escape corruption."""
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
            found.append((start + match.start(), "BARE_SPACING_COMMAND", match[0]))
    return tuple(MathFormatIssue(code, offset, text.count("\n", 0, offset) + 1,
                                 offset - text.rfind("\n", 0, offset), token)
                 for offset, code, token in sorted(found))



def require_math_format(text: str, *, markdown: bool = True) -> None:
    issues = math_format_issues(text, markdown=markdown)
    if issues:
        raise ValueError("Math formatting requires correction: " + "; ".join(
            f"{issue.line}:{issue.column} {issue.code}" for issue in issues
        ))
