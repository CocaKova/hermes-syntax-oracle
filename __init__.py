"""syntax-oracle — deterministic diagnosis for Python SyntaxErrors in tool results.

Why this exists
---------------
Small local models are unreliable at counting brackets, and they cannot tell
that they are unreliable at it. When ``python3`` says
``closing parenthesis ']' does not match opening parenthesis '{'`` on a line
the model "knows" is balanced, the model resolves the contradiction by
blaming something it cannot see — tool escaping, shell quoting, the linter,
the Python build — and spends many turns proving the file is byte-clean
instead of finding the missing ``}``.

The fix is not to ask the model to count harder. It is to hand it the fact
it cannot compute. This plugin watches tool results for a Python
``SyntaxError`` / ``IndentationError`` / ``TabError``, runs the stdlib
tokenizer over the source, and appends a short, deterministic verdict to the
tool result — which delimiter is unclosed or surplus, at which line and
column, with carets under the line, and what the fix is.

Design rules
------------
* **Silence is the safe failure.** The plugin speaks only when the
  tokenizer's finding is *consistent with* Python's own error message
  (same delimiter characters, same line). Any doubt → return None and the
  tool result passes through untouched. A wrong diagnosis would be worse
  than none — it would become a new thing for the model to be confused by.
* **Observational.** Uses the ``transform_tool_result`` hook (the same seam
  upstream's ``security-guidance`` plugin uses). Nothing is blocked; the
  file is still written, the command still ran. The verdict rides back to
  the model in the next turn's tool message.
* **Cheap.** A substring miss for every tool call that has no SyntaxError.
  When it fires: read one file (≤512 KiB) and tokenize it. Milliseconds.
* **Brain-agnostic and update-proof.** Lives in ``~/.hermes/plugins/``;
  no source patch, no gateway code touched. Stdlib only.

Disable with ``SYNTAX_ORACLE_DISABLE=1``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tokenize
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

__version__ = "0.2.0"

logger = logging.getLogger(__name__)

_MAX_SOURCE_BYTES = 512 * 1024
_MAX_LINE_SHOWN = 140          # window long lines around the marks
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}
_NAMES = {"(": "parenthesis", "[": "bracket", "{": "brace",
          ")": "parenthesis", "]": "bracket", "}": "brace"}

# Traceback shape:
#   File "<path>", line N            (no ", in <frame>" suffix — that is a runtime frame)
#       <echoed line>                (optional)
#       ^^^^                         (optional caret line)
#   SyntaxError: msg
_TB_RE = re.compile(
    r'File "(?P<file>[^"\n]+)", line (?P<line>\d+)[ \t]*\n'
    r'(?:(?P<code>[^\n]*)\n)?'
    r'(?:[ \t]*\^+[^\n]*\n)?'
    r'(?P<kind>SyntaxError|IndentationError|TabError): (?P<msg>[^\n]+)'
)
_CARET_RE = re.compile(r'\n(?P<pad>[ \t]*)\^')
# In-process lint shape (Hermes write_file / patch):  SyntaxError: msg (line N, column M)
_LINT_RE = re.compile(
    r'(?P<kind>SyntaxError|IndentationError|TabError): (?P<msg>.+?) '
    r'\(line (?P<line>\d+), column (?P<col>\d+|None)\)'
)

_MSG_MISMATCH = re.compile(
    r"closing parenthesis '(?P<closer>[\)\]\}])' does not match opening parenthesis "
    r"'(?P<opener>[\(\[\{])'(?: on line (?P<oline>\d+))?")
_MSG_NEVER_CLOSED = re.compile(r"'(?P<opener>[\(\[\{])' was never closed")
_MSG_UNMATCHED = re.compile(r"unmatched '(?P<closer>[\)\]\}])'")
_MSG_EOF = re.compile(r"unexpected EOF while parsing|EOF in multi-line statement")
_MSG_EXPECTED_BLOCK = re.compile(r"expected an indented block after '(?P<stmt>[^']+)' statement on line (?P<sline>\d+)")

# Static hints for messages that need no analysis but routinely send small
# models down the wrong road. Keyed by a substring of Python's message.
_STATIC_HINTS: List[Tuple[str, str]] = [
    ("f-string expression part cannot include a backslash",
     "The `{...}` part of an f-string may not contain a backslash on this Python "
     "(<3.12). Compute the escaped value into a variable first (`q = json.dumps(x)`; "
     "`f\"...{q}...\"`) or use `.format()`. This is a language rule, not tool escaping."),
    ("f-string: expecting '}'",
     "A `{` inside an f-string opens a replacement field that is never closed. To write a "
     "literal brace in an f-string, double it: `{{` / `}}`."),
    ("f-string: single '}' is not allowed",
     "A lone `}` inside an f-string. To write a literal brace in an f-string, double it: `}}`."),
    ("unterminated triple-quoted string literal",
     "A `\"\"\"`/`'''` opened at the reported line+column has no closing partner anywhere "
     "later in the file. Everything after it was swallowed into the string."),
    ("unterminated string literal",
     "The quote at the reported line+column is never closed on that line. Look for a stray "
     "or mismatched `'`/`\"` inside the string, or a missing closing quote."),
    ("Perhaps you forgot a comma",
     "Two expressions sit next to each other with nothing between them on the reported "
     "line — usually a missing `,` in a list/dict/call, or a missing operator."),
    ("cannot assign to",
     "Left side of `=` is not assignable — often `==` was intended (comparison), or a call "
     "result / literal sits on the left."),
    ("invalid decimal literal",
     "A number runs straight into a name (e.g. `2x`, `1st`, `0x`) — usually a missing space "
     "or operator, or a name that starts with a digit."),
]


# ---------------------------------------------------------------------------
# Tokenizer-based delimiter analysis
# ---------------------------------------------------------------------------

def _delimiter_events(source: str) -> Tuple[List[Tuple], List[Tuple[str, int, int]]]:
    """Return (problems, unclosed_at_end).

    problems entries:
        ("mismatch", closer, cl, cc, opener, ol, oc)  — closer arrived while a
                                                        different opener was on top
        ("extra",    closer, cl, cc)                  — closer with empty stack
    unclosed entries: (opener, line, col)
    Lines are 1-based, cols 0-based (tokenize convention).

    tokenize is lenient about mismatched brackets — it yields the whole
    stream and only raises at EOF — so the stack bookkeeping happens here.
    """
    stack: List[Tuple[str, int, int]] = []
    problems: List[Tuple] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.OP:
                continue
            s = tok.string
            if s in _OPENERS:
                stack.append((s, tok.start[0], tok.start[1]))
            elif s in _CLOSERS:
                if not stack:
                    problems.append(("extra", s, tok.start[0], tok.start[1]))
                    continue
                top = stack[-1]
                if _OPENERS[top[0]] == s:
                    stack.pop()
                else:
                    problems.append(("mismatch", s, tok.start[0], tok.start[1], top[0], top[1], top[2]))
                    # Recover the way a reader would: assume the closer was
                    # meant for the nearest matching opener below, if any.
                    for i in range(len(stack) - 1, -1, -1):
                        if _OPENERS[stack[i][0]] == s:
                            del stack[i:]
                            break
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # EOF inside a multi-line statement, or an unterminated string —
        # whatever was collected so far is still meaningful.
        pass
    except Exception:  # noqa: BLE001 — never let diagnosis break a tool result
        return [], []
    return problems, stack


def _reline(p: Tuple, lineno: int) -> Tuple:
    """Single-echoed-line mode: the tokenizer says line 1; the traceback knows better."""
    if p[0] == "mismatch":
        _, c, _cl, cc, o, _ol, oc = p
        return ("mismatch", c, lineno, cc, o, lineno, oc)
    _, c, _cl, cc = p
    return ("extra", c, lineno, cc)


def _lines(source: str) -> List[str]:
    return source.splitlines()


def _line_of(source: str, lineno: int) -> str:
    ls = _lines(source)
    return ls[lineno - 1] if 1 <= lineno <= len(ls) else ""


def _visible_indent(line: str) -> str:
    """Render leading whitespace visibly: tab → '→', space → '·'."""
    out = []
    for ch in line:
        if ch == "\t":
            out.append("→")
        elif ch == " ":
            out.append("·")
        else:
            break
    return "".join(out)


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _mark(line: str, marks: List[Tuple[int, str]]) -> str:
    """Render `line` with a caret line beneath. marks = [(col0, label)].

    Tabs are expanded so carets align; long lines are windowed around the
    marks (columns quoted in the prose stay true to the real line).
    """
    marks = sorted(marks)
    # Expand tabs (tabsize 8, like the terminal) while remapping columns.
    expanded, colmap, vis = [], [], 0
    for i, ch in enumerate(line):
        colmap.append(vis)
        if ch == "\t":
            w = 8 - (vis % 8)
            expanded.append(" " * w)
            vis += w
        else:
            expanded.append(ch)
            vis += 1
    colmap.append(vis)
    text = "".join(expanded).rstrip()
    vmarks = [(colmap[min(c, len(colmap) - 1)], lbl) for c, lbl in marks]

    # Window long lines around the marks.
    lo, hi = 0, len(text)
    if len(text) > _MAX_LINE_SHOWN and vmarks:
        first, last = vmarks[0][0], vmarks[-1][0]
        span = last - first
        pad = max(10, (_MAX_LINE_SHOWN - span) // 2)
        lo = max(0, first - pad)
        hi = min(len(text), last + pad)
        if hi - lo < _MAX_LINE_SHOWN:  # use the budget
            lo = max(0, hi - _MAX_LINE_SHOWN)
    shown = text[lo:hi]
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    offset = len(prefix) - lo

    # One caret row when labels fit side by side; otherwise each mark gets its
    # own row (carets on the first row, labels hanging below), so nothing is dropped.
    cols = [c + offset for c, _ in vmarks]
    rows: List[str] = []
    fits = True
    cursor = 0
    for (c, label) in zip(cols, [lbl for _, lbl in vmarks]):
        if c < cursor:
            fits = False
            break
        cursor = c + 1 + (len(label) + 1 if label else 0)
    if fits:
        cursor, row = 0, []
        for c, (_, label) in zip(cols, vmarks):
            row.append(" " * (c - cursor) + "^" + (" " + label if label else ""))
            cursor = c + 1 + (len(label) + 1 if label else 0)
        rows.append("".join(row))
    else:
        carets, cursor = [], 0
        for c in cols:
            if c >= cursor:
                carets.append(" " * (c - cursor) + "^")
                cursor = c + 1
        rows.append("".join(carets))
        for c, (_, label) in reversed(list(zip(cols, vmarks))):
            if label:
                rows.append(" " * c + "└ " + label)
    body = "\n      ".join(rows)
    return f"      {prefix}{shown}{suffix}\n      {body}"


# ---------------------------------------------------------------------------
# Diagnosis (structured) — this is what the fuzzer checks
# ---------------------------------------------------------------------------

def diagnose(source: str, msg: str, err_line: int, whole_file: bool = True,
             err_col: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return a structured finding consistent with Python's message, or None.

    Finding kinds (all positions 1-based line, 0-based col):
      {"kind": "mismatch", "closer": c, "closer_pos": (l, c0), "opener": o, "opener_pos": (l, c0)}
      {"kind": "never_closed", "opener": o, "opener_pos": (l, c0), "also_open": [(o,l,c0)...]}
      {"kind": "extra", "closer": c, "closer_pos": (l, c0)}
      {"kind": "eof_open", "open": [(o,l,c0)...]}
      {"kind": "tabs", "line": l, "indent": "<visible>", "expected": "<visible>"}
      {"kind": "unindent", "line": l, "indent_len": n, "levels": [..]}
      {"kind": "unexpected_indent", "line": l, "prev_line": l2, "prev_indent_len": n, "indent_len": m}
    `whole_file=False` means `source` is only the echoed offending line, so
    only intra-line findings are trusted and the line number is `err_line`.
    """
    if msg.startswith("f-string"):
        # Inside f-strings the tokenizer's view of `{}` fields is not a plain
        # bracket stack; go straight to the fix-gated path.
        return _diagnose_generic(source, msg, err_line, err_col) if whole_file else None

    problems, unclosed = _delimiter_events(source)
    if not whole_file:
        problems = [_reline(p, err_line) for p in problems]
        unclosed = [(u[0], err_line, u[2]) for u in unclosed]
        if err_col is None and len([p for p in problems]) > 1:
            return None  # several candidates on the line and no caret to disambiguate

    m = _MSG_MISMATCH.search(msg)
    if m:
        closer, opener = m.group("closer"), m.group("opener")
        oline = int(m.group("oline")) if m.group("oline") else None
        for p in problems:
            if p[0] != "mismatch":
                continue
            _, c, cl, cc, o, ol, oc = p
            if c != closer or o != opener or cl != err_line:
                continue
            if err_col is not None and cc != err_col:
                continue
            if whole_file and oline is not None and ol != oline:
                continue
            if not whole_file and oline is not None and oline != err_line:
                continue  # opener on another line; can't see it
            return {"kind": "mismatch", "closer": c, "closer_pos": (cl, cc),
                    "opener": o, "opener_pos": (ol, oc)}
        return None

    m = _MSG_NEVER_CLOSED.search(msg)
    if m:
        opener = m.group("opener")
        cands = [u for u in unclosed if u[0] == opener and u[1] == err_line]
        if err_col is not None:
            cands = [u for u in cands if u[2] == err_col] or ([] if whole_file else cands[:1])
        if not cands:
            return None
        o, ol, oc = cands[0]  # outermost unclosed of that kind on the line
        others = [u for u in unclosed if u != cands[0]]
        return {"kind": "never_closed", "opener": o, "opener_pos": (ol, oc), "also_open": others[:3]}

    m = _MSG_UNMATCHED.search(msg)
    if m:
        closer = m.group("closer")
        for p in problems:
            if err_col is not None and p[3] != err_col:
                continue
            if p[0] == "extra" and p[1] == closer and p[2] == err_line:
                return {"kind": "extra", "closer": p[1], "closer_pos": (p[2], p[3])}
            if p[0] == "mismatch" and p[1] == closer and p[2] == err_line:
                # Python reports "unmatched" when the closer has no partner
                # anywhere below on the stack; our recovery saw a different
                # opener on top. Report the closer as surplus — same fix.
                return {"kind": "extra", "closer": p[1], "closer_pos": (p[2], p[3])}
        return None

    if _MSG_EOF.search(msg) and whole_file and unclosed:
        return {"kind": "eof_open", "open": unclosed[:3]}

    if not whole_file:
        return None
    ind = _diagnose_indent(source, msg, err_line)
    if ind:
        return ind
    return _diagnose_generic(source, msg, err_line, err_col)


def _stack_at(source: str, line: int, col: Optional[int]) -> List[Tuple[str, int, int]]:
    """Delimiter stack just before position (line, col) — col None = end of line."""
    stack: List[Tuple[str, int, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.start[0] > line or (tok.start[0] == line and col is not None and tok.start[1] >= col):
                break
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPENERS:
                stack.append((tok.string, tok.start[0], tok.start[1]))
            elif tok.string in _CLOSERS and stack and _OPENERS[stack[-1][0]] == tok.string:
                stack.pop()
    except Exception:  # noqa: BLE001
        pass
    return stack


def _diagnose_generic(source: str, msg: str, err_line: int, err_col: Optional[int]) -> Optional[Dict[str, Any]]:
    """Messages the specific matchers don't cover. Findings here carry
    requires_fix=True: they are only reported if repair() proves a one-edit fix."""
    if "f-string" in msg:
        if ("single '}' is not allowed" in msg or "expecting '}'" in msg or "expecting '='" in msg
                or "unmatched" in msg or "closing parenthesis" in msg or "was never closed" in msg
                or "invalid syntax" in msg):
            return {"kind": "fstring_brace", "line": err_line, "col": err_col, "msg": msg, "requires_fix": True}
        return None
    if msg.startswith("invalid syntax") or "expected ':'" in msg or "was never closed" in msg or "unexpected EOF" in msg:
        st = _stack_at(source, err_line, err_col)
        if st:
            o, ol, oc = st[-1]
            return {"kind": "open_at_error", "opener": o, "opener_pos": (ol, oc),
                    "line": err_line, "col": err_col, "requires_fix": True}
    return None


def _diagnose_indent(source: str, msg: str, err_line: int) -> Optional[Dict[str, Any]]:
    ls = _lines(source)
    if not (1 <= err_line <= len(ls)):
        return None
    line = ls[err_line - 1]
    ws = _leading_ws(line)

    def prev_code_lines(n: int) -> List[Tuple[int, str]]:
        out = []
        i = err_line - 2
        while i >= 0 and len(out) < n:
            if ls[i].strip() and not ls[i].lstrip().startswith("#"):
                out.append((i + 1, ls[i]))
            i -= 1
        return out

    if "inconsistent use of tabs and spaces" in msg:
        prev = prev_code_lines(1)
        exp = _visible_indent(prev[0][1]) if prev else ""
        return {"kind": "tabs", "line": err_line, "indent": _visible_indent(line), "expected": exp,
                "prev_line": prev[0][0] if prev else None}

    if "unindent does not match any outer indentation level" in msg:
        levels = sorted({len(_leading_ws(l)) for _n, l in prev_code_lines(40)})
        return {"kind": "unindent", "line": err_line, "indent_len": len(ws), "indent": _visible_indent(line),
                "levels": levels}

    if "unexpected indent" in msg:
        prev = prev_code_lines(1)
        if not prev:
            return None
        pn, pl = prev[0]
        return {"kind": "unexpected_indent", "line": err_line, "indent_len": len(ws), "indent": _visible_indent(line),
                "prev_line": pn, "prev_indent_len": len(_leading_ws(pl)), "prev_ends_colon": pl.rstrip().endswith(":")}

    m = _MSG_EXPECTED_BLOCK.search(msg)
    if m:
        sline = int(m.group("sline"))
        return {"kind": "expected_block", "line": err_line, "stmt": m.group("stmt"), "stmt_line": sline,
                "indent_len": len(ws), "stmt_indent_len": len(_leading_ws(ls[sline - 1])) if 1 <= sline <= len(ls) else None}
    return None



# ---------------------------------------------------------------------------
# Repair search — try the one-character edits the finding implies, keep the
# first that makes the whole file compile. Bounded; silent when nothing works.
# ---------------------------------------------------------------------------

_MAX_REPAIR_COMPILES = 160          # for sources ≤ 50 KiB; halves above that
_MAX_REPAIR_SECONDS = 1.5           # wall-clock cap for the repair search (tool path is synchronous)
_MAX_REPAIR_SOURCE = 200 * 1024
_REPAIR_LOOKBACK_LINES = 40


def _compiles(src: str) -> bool:
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(src, "<syntax-oracle>", "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError):
        return False


def _offsets(source: str) -> List[int]:
    starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _off(starts: List[int], line: int, col: int) -> int:
    if not (1 <= line <= len(starts)):
        return starts[-1] if starts else 0
    return starts[line - 1] + col


def _delim_tokens(source: str) -> List[Tuple[str, int, int]]:
    """All delimiter tokens (char, line, col0) in source order; tolerant of errors."""
    out: List[Tuple[str, int, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.OP and (tok.string in _OPENERS or tok.string in _CLOSERS):
                out.append((tok.string, tok.start[0], tok.start[1]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _token_boundaries(source: str, line_lo: int, line_hi: int) -> List[Tuple[int, int]]:
    """(line, col0) positions right after each token on lines in [lo, hi]; source order."""
    out: List[Tuple[int, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.OP, tokenize.NAME, tokenize.NUMBER, tokenize.STRING):
                if line_lo <= tok.end[0] <= line_hi:
                    out.append(tok.end)
                if tok.start[0] > line_hi:
                    break
    except Exception:  # noqa: BLE001
        pass
    return out


def _name_interiors(source: str, line_lo: int, line_hi: int) -> List[Tuple[int, int]]:
    """Positions strictly inside NAME tokens on the given lines (a deleted `(`/`[`
    can fuse two identifiers: `os.makedirsself.x`, `dns.Queryname=`).

    Ordered by evidence: splits whose left part is a name that occurs elsewhere
    in the file come first (`_format_callback|func` when `_format_callback` is
    defined above) — that is a real signal, not a guess. Then the rest,
    last-line-first, right-to-left."""
    names: set = set()
    cands: List[Tuple[int, int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
                if line_lo <= tok.start[0] <= line_hi and len(tok.string) > 2:
                    l, c0 = tok.start
                    cands.extend((l, c0 + i, tok.string[:i]) for i in range(1, len(tok.string)))
    except Exception:  # noqa: BLE001
        pass
    known = [(l, c, prefix) for l, c, prefix in cands if prefix in names and len(prefix) > 1]
    known.sort(key=lambda t: (-len(t[2]), -t[0], -t[1]))
    rest = [(l, c) for l, c, prefix in cands if not (prefix in names and len(prefix) > 1)]
    return [(l, c) for l, c, _p in known] + rest[::-1]


_CONTINUATION_OPS = frozenset((",", "\\", "+", "-", "*", "/", "%", "&", "|", "^", "=", "==", "!=", "<", ">",
                               "<=", ">=", "->", "**", "//", "@", "|=", "+=", "-=", ":="))
_CONTINUATION_KEYWORDS = frozenset(("and", "or", "not", "in", "is", "if", "else", "for", "return", "yield",
                                    "await", "lambda", "import", "from", "as", "assert", "del", "raise", "with",
                                    "while", "elif", "except"))


def _indent_anomalies(source: str) -> List[Tuple[str, str, int, int]]:
    """Places where indentation contradicts the delimiter stack — how a human
    scans a 400-line dict for the missing brace.

    Returns [(kind, char, line, col0)]:
      ("closer", "}", L, C)  — line L is indented no deeper than the line that
                               opened the innermost still-open `{`, yet doesn't
                               close it → a `}` probably belongs at (L-ish, C) =
                               end of the previous code line
      ("opener", "", L, C)   — line L is indented deeper than the previous code
                               line, which ends in a way that cannot introduce a
                               deeper line (`:` inside brackets, a bare name…)
                               → an opener probably belongs at (L_prev, C) = its end
    Ordered by position. Bounded; candidates only — repair() compiles to confirm.
    """
    out: List[Tuple[str, str, int, int]] = []
    stack: List[Tuple[str, int, int]] = []       # (char, line, line_indent)
    prev_line = 0
    prev_end_col = 0
    prev_last_tok: Optional[tokenize.TokenInfo] = None
    line_indent: Dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING):
                continue
            l, c = tok.start
            if l not in line_indent:
                line_indent[l] = c
                if stack:
                    o, ol, oi = stack[-1]
                    if l > ol and c <= oi and not (tok.type == tokenize.OP and tok.string in _CLOSERS):
                        out.append(("closer", _OPENERS[o], prev_line, prev_end_col))
                if prev_last_tok is not None and l > prev_line and c > line_indent.get(prev_line, 0):
                    pt = prev_last_tok
                    # A deeper line is normal after an opener, a comma, an operator, or a
                    # keyword that takes an expression. After anything else (a bare name, a
                    # closer, a `:` inside brackets) it is suspicious — inside brackets only,
                    # since outside them INDENT tokens make Python's own error precise.
                    ends_ok = pt.type == tokenize.OP and (pt.string in _OPENERS or pt.string in _CONTINUATION_OPS)
                    is_kw = pt.type == tokenize.NAME and pt.string in _CONTINUATION_KEYWORDS
                    if stack and not ends_ok and not is_kw:
                        out.append(("opener", "", prev_line, prev_end_col))
            if tok.type == tokenize.OP:
                if tok.string in _OPENERS:
                    stack.append((tok.string, l, line_indent[l]))
                elif tok.string in _CLOSERS and stack and _OPENERS[stack[-1][0]] == tok.string:
                    stack.pop()
            prev_line, prev_end_col = tok.end
            prev_last_tok = tok
    except Exception:  # noqa: BLE001
        pass
    return out


def _unbalanced_lines(toks: List[Tuple[str, int, int]], opener: str, lo: int, hi: int, sign: int) -> List[int]:
    """Lines in [lo, hi] whose own count of `opener` minus its closer has the given
    sign (+1: an opener too many → a closer is probably missing there; -1: a closer
    too many → an opener is probably missing there). A deleted delimiter almost
    always leaves its line unbalanced, so these lines are searched first."""
    net: Dict[int, int] = {}
    closer = _OPENERS[opener]
    for ch, l, _c in toks:
        if lo <= l <= hi and ch in (opener, closer):
            net[l] = net.get(l, 0) + (1 if ch == opener else -1)
    return [l for l, v in net.items() if (v > 0 if sign > 0 else v < 0)]


def _known_splits(source: str, line_lo: int, line_hi: int) -> List[Tuple[int, int]]:
    """Just the evidence-backed head of _name_interiors (prefix is a known name)."""
    names: set = set()
    cands: List[Tuple[int, int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
                if line_lo <= tok.start[0] <= line_hi and len(tok.string) > 2:
                    l, c0 = tok.start
                    cands.extend((l, c0 + i, tok.string[:i]) for i in range(2, len(tok.string)))
    except Exception:  # noqa: BLE001
        pass
    known = [(l, c, prefix) for l, c, prefix in cands if prefix in names]
    known.sort(key=lambda t: (-len(t[2]), -t[0], -t[1]))   # longest known prefix = strongest evidence
    return [(l, c) for l, c, _p in known]


def repair(finding: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """Return {"action": "insert"|"delete"|"replace"|"reindent", "char", "pos": (line, col0), "desc"}
    for the first candidate edit that makes `source` compile, else None."""
    if len(source) > _MAX_REPAIR_SOURCE:
        return None
    starts = _offsets(source)
    lines = source.split("\n")
    k = finding["kind"]
    cands: List[Tuple[str, str, Tuple[int, int], str]] = []  # (action, char, pos, new_source)

    def ins(ch: str, line: int, col: int) -> None:
        o = _off(starts, line, col)
        cands.append(("insert", ch, (line, col), source[:o] + ch + source[o:]))

    def dele(line: int, col: int) -> None:
        o = _off(starts, line, col)
        cands.append(("delete", source[o:o + 1], (line, col), source[:o] + source[o + 1:]))

    def repl(ch: str, line: int, col: int) -> None:
        o = _off(starts, line, col)
        cands.append(("replace", ch, (line, col), source[:o] + ch + source[o + 1:]))

    def eol(line: int) -> int:
        return len(lines[line - 1].rstrip()) if 1 <= line <= len(lines) else 0

    toks = _delim_tokens(source)
    anomalies = _indent_anomalies(source)

    # Only the first few anomalies after a fault are meaningful — once the stack
    # is off by one, every later line "contradicts" it too (echoes).
    def anomaly_closers(want: str, lo: Tuple[int, int], hi: Tuple[int, int], limit: int = 4) -> None:
        hits = [(l, col) for kind_, ch, l, col in anomalies if kind_ == "closer" and ch == want and lo <= (l, col) <= hi]
        for l, col in hits[:limit]:
            ins(want, l, col)

    def anomaly_openers(kinds: str, lo: Tuple[int, int], hi: Tuple[int, int], limit: int = 4) -> None:
        hits = [(l, col) for kind_, _ch, l, col in anomalies if kind_ == "opener" and lo <= (l, col) <= hi]
        for l, col in hits[-limit:][::-1]:  # nearest to the error first
            for opener in kinds:
                ins(opener, l, col)

    if k == "extra":
        c, (cl, cc) = finding["closer"], finding["closer_pos"]
        dele(cl, cc)
        anomaly_openers(_CLOSERS[c], (1, 0), (cl, cc))
        for l in _unbalanced_lines(toks, _CLOSERS[c], max(1, cl - 80), cl, -1)[::-1][:4]:
            for l2, c2 in reversed(_token_boundaries(source, l, l)):
                if (l2, c2) < (cl, cc):
                    ins(_CLOSERS[c], l2, c2)
        # an earlier closer of the same kind may be the real surplus one
        for ch, l, col in [t for t in toks if t[0] == c and (t[1], t[2]) < (cl, cc)][::-1][:30]:
            dele(l, col)
        # or an opener is missing: a fused identifier with a known prefix, then token
        # boundaries on this line and the 3 above (nearest first) ...
        if c in ")]":
            for l, col in _known_splits(source, max(1, cl - 3), cl):
                if (l, col) < (cl, cc):
                    ins(_CLOSERS[c], l, col)
        for l, col in reversed(_token_boundaries(source, max(1, cl - 3), cl)):
            if (l, col) < (cl, cc):
                ins(_CLOSERS[c], l, col)
        # ... or it fused two identifiers on this line ...
        if c in ")]":
            for l, col in _name_interiors(source, cl, cl):
                if (l, col) < (cl, cc):
                    ins(_CLOSERS[c], l, col)
        # ... or it is further up in a multi-line construct
        for l, col in reversed(_token_boundaries(source, max(1, cl - _REPAIR_LOOKBACK_LINES), cl - 4)):
            ins(_CLOSERS[c], l, col)

    elif k == "mismatch":
        c, (cl, cc) = finding["closer"], finding["closer_pos"]
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        want = _OPENERS[o]
        ins(want, cl, cc)            # add the missing closer right here
        repl(want, cl, cc)           # or this closer is simply the wrong kind
        anomaly_closers(want, (ol, oc), (cl, cc))
        anomaly_openers(_CLOSERS[c], (ol, oc), (cl, cc))
        for l in _unbalanced_lines(toks, o, ol, cl, +1)[:6]:          # a `want` is missing on that line
            for l2, c2 in reversed(_token_boundaries(source, l, l)):
                if (ol, oc) < (l2, c2) <= (cl, cc):
                    ins(want, l2, c2)
        for l in _unbalanced_lines(toks, _CLOSERS[c], ol, cl, -1)[:6]:  # an opener of c's kind is missing there
            for l2, c2 in reversed(_token_boundaries(source, l, l)):
                if (ol, oc) < (l2, c2) < (cl, cc):
                    ins(_CLOSERS[c], l2, c2)
        dele(cl, cc)                 # or this closer is surplus
        dele(ol, oc)                 # or the opener is surplus
        repl(_CLOSERS[c], ol, oc)    # or the opener is the wrong kind
        # or an earlier same-kind closer / opener between opener and closer is the culprit
        between = [t for t in toks if (ol, oc) < (t[1], t[2]) < (cl, cc)]
        for ch, l, col in reversed(between):
            if ch == c or ch == o or ch in _CLOSERS:
                dele(l, col)
        # or the missing closer belongs earlier: at a token boundary on the opener's line
        # (from the end backwards), then at the end of each line between opener and closer
        for l, c2 in reversed(_token_boundaries(source, ol, ol)):
            if (l, c2) > (ol, oc):
                ins(want, l, c2)
        # or the closer's own opener is missing: on the closer's line, at the end of a
        # line between, right after the opener, or mid-line between (nearest the closer first)
        if c in ")]":
            for l, c2 in _known_splits(source, max(ol, cl - 3), cl):
                if (ol, oc) < (l, c2) < (cl, cc):
                    ins(_CLOSERS[c], l, c2)
        for l, c2 in reversed(_token_boundaries(source, cl, cl)):
            if (l, c2) < (cl, cc) and (l, c2) > (ol, oc):
                ins(_CLOSERS[c], l, c2)
        for l in range(cl - 1, ol - 1, -1):
            if l >= cl - _REPAIR_LOOKBACK_LINES:
                ins(_CLOSERS[c], l, eol(l))
        for l, c2 in _token_boundaries(source, ol, ol):
            if (l, c2) > (ol, oc):
                ins(_CLOSERS[c], l, c2)
        for l, c2 in reversed(_token_boundaries(source, max(ol, cl - 12), cl - 1)):
            if (ol, oc) < (l, c2) < (cl, cc):
                ins(_CLOSERS[c], l, c2)
        if c in ")]":
            for l, col in _name_interiors(source, cl, cl):
                if (ol, oc) < (l, col) < (cl, cc):
                    ins(_CLOSERS[c], l, col)
        for l in range(cl - 1, max(ol, cl - _REPAIR_LOOKBACK_LINES) - 1, -1):
            ins(want, l, eol(l))
        # or the missing closer sits mid-line somewhere between (nearest the closer first)
        for l, c2 in reversed(_token_boundaries(source, max(ol, cl - 12), cl)):
            if (ol, oc) < (l, c2) < (cl, cc):
                ins(want, l, c2)

    elif k == "never_closed":
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        want = _OPENERS[o]
        n = len(lines)
        # 0. where indentation says the construct should have closed
        anomaly_closers(want, (ol, oc), (n + 1, 0))
        # 0b. lines that have one `opener` too many — a deleted closer leaves its line unbalanced
        for l in _unbalanced_lines(toks, o, ol + 1, min(n, ol + 80), +1)[:4]:
            for l2, c2 in reversed(_token_boundaries(source, l, l)):
                ins(want, l2, c2)
        # 1. the opener's own line: end, then each token boundary after the opener
        ins(want, ol, eol(ol))
        for l, c2 in reversed(_token_boundaries(source, ol, ol)):
            if (l, c2) > (ol, oc):
                ins(want, l, c2)
        # 2. probable statement ends: lines after which indentation drops back to
        #    the opener line's level (or EOF) — that's where a closer usually belongs
        base = len(_leading_ws(lines[ol - 1]))
        for l in range(ol + 1, min(n, ol + 200) + 1):
            if not lines[l - 1].strip():
                continue
            nxt = next((lines[j - 1] for j in range(l + 1, n + 1) if lines[j - 1].strip()), None)
            if nxt is None or len(_leading_ws(nxt)) <= base:
                ins(want, l, eol(l))
        # 3. mid-line positions on the next few lines
        for l, c2 in _token_boundaries(source, ol + 1, min(n, ol + 12)):
            ins(want, l, c2)
        # 4. every line end for a while, then EOF
        for l in range(ol + 1, min(n, ol + 60) + 1):
            ins(want, l, eol(l))
        cands.append(("insert", want, (n, eol(n)), source + want))
        # 5. or the opener itself is the surplus
        dele(ol, oc)
        # a duplicated opener on the same line
        for ch, l, col in [t for t in toks if t[0] == o and t[1] == ol and t[2] != oc]:
            dele(l, col)

    elif k == "open_at_error":
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        want = _OPENERS[o]
        line, col = finding["line"], finding["col"]
        anomaly_closers(want, (ol, oc), (line + 1, 0))
        if col is not None:
            ins(want, line, col)
            # also right after the previous token (Python's caret often sits one token late)
            for l, c2 in reversed(_token_boundaries(source, max(1, line - 2), line)):
                if (l, c2) <= (line, col):
                    ins(want, l, c2)
                    break
        for l in range(ol, min(len(lines), line) + 1):
            ins(want, l, eol(l))
        dele(ol, oc)

    elif k == "fstring_brace":
        line, col = finding["line"], finding["col"]
        body = lines[line - 1] if 1 <= line <= len(lines) else ""
        if col is not None:
            dele(line, col)
            if "single '}'" in finding["msg"]:
                # a `{` is missing somewhere earlier on the line: try each position back to the string start
                for c2 in range(col - 1, max(-1, col - 60), -1):
                    ins("{", line, c2)
            else:  # a field is not closed: try `}` at each nearby position (forward, then back)
                for c2 in range(col, min(len(body), col + 50) + 1):
                    ins("}", line, c2)
                for c2 in range(col - 1, max(-1, col - 50), -1):
                    ins("}", line, c2)
        else:
            # no column: try deleting each brace on the line, then closing before each quote
            for i, ch in enumerate(body):
                if ch in "{}":
                    dele(line, i)

    elif k == "eof_open":
        tail = "".join(_OPENERS[u[0]] for u in reversed(finding["open"]))
        cands.append(("insert", tail, (len(lines), eol(len(lines))), source + tail))

    elif k in ("unexpected_indent", "unindent", "tabs"):
        line = finding["line"]
        body = lines[line - 1] if 1 <= line <= len(lines) else ""
        stripped = body.lstrip(" \t")
        def reindent(ws: str, desc: str) -> None:
            new = lines[:]
            new[line - 1] = ws + stripped
            cands.append(("reindent", desc, (line, 0), "\n".join(new)))
        if k == "unexpected_indent":
            reindent(" " * finding["prev_indent_len"], f"dedent to {finding['prev_indent_len']}")
            # or a bracket is missing on the previous code line(s)
            pl = finding["prev_line"]
            for bl in range(pl + 1, line):        # a blank line that used to hold a lone opener
                if 1 <= bl <= len(lines) and not lines[bl - 1].strip():
                    for opener in "([{":
                        ins(opener, bl, len(lines[bl - 1]))
            anomaly_openers("([{", (max(1, pl - 3), 0), (line, 0))
            for ch, l, col in [t for t in toks if t[1] == pl and t[0] in _CLOSERS][::-1]:
                dele(l, col)
            for l, col in _known_splits(source, pl, pl):                     # `_format_callback|func`
                ins("(", l, col)
                ins("[", l, col)
            for l, col in reversed(_token_boundaries(source, pl, pl)):      # previous line, right to left
                for opener in "([{":
                    ins(opener, l, col)
            for l, col in _name_interiors(source, pl, pl):                   # fused identifier on it
                ins("(", l, col)
                ins("[", l, col)
            for l, col in reversed(_token_boundaries(source, max(1, pl - 3), pl - 1)):
                for opener in "([{":
                    ins(opener, l, col)
        elif k == "unindent":
            for lv in sorted(finding["levels"], key=lambda x: abs(x - finding["indent_len"])):
                reindent(" " * lv, f"re-indent to {lv}")
        else:  # tabs
            prev_ws = None
            for l in range(line - 1, 0, -1):
                if lines[l - 1].strip():
                    prev_ws = _leading_ws(lines[l - 1])
                    break
            if prev_ws is not None:
                reindent(prev_ws, "re-indent like the line above")
            reindent(_leading_ws(body).expandtabs(8), "expand tabs to spaces")
            reindent(_leading_ws(body).expandtabs(4), "expand tabs to spaces (4)")

    import time as _time
    budget = _MAX_REPAIR_COMPILES if len(source) <= 50 * 1024 else _MAX_REPAIR_COMPILES // 2
    deadline = _time.monotonic() + _MAX_REPAIR_SECONDS
    seen: set = set()
    tried = 0
    for action, ch, pos, new_src in cands:
        key = (action, ch, pos)
        if key in seen:
            continue
        seen.add(key)
        tried += 1
        if tried > budget or _time.monotonic() > deadline:
            break
        if _compiles(new_src):
            return {"action": action, "char": ch, "pos": pos}
    return None


def _render_fix(fix: Dict[str, Any], source: str) -> str:
    l, c = fix["pos"]
    line = _line_of(source, l)
    a = fix["action"]
    if a == "insert":
        head = f"insert `{fix['char']}` at line {l} col {c + 1}"
        # show the line with a placeholder at the insertion point
        label = "insert `" + fix["char"] + "` here"
        return f"{head}\n{_mark(line[:c] + '▁' + line[c:], [(c, label)])}"
    if a == "delete":
        head = f"delete the `{fix['char']}` at line {l} col {c + 1}"
        return f"{head}\n{_mark(line, [(c, 'delete this')])}"
    if a == "replace":
        head = f"replace the `{line[c:c + 1]}` at line {l} col {c + 1} with `{fix['char']}`"
        label = "→ `" + fix["char"] + "`"
        return f"{head}\n{_mark(line, [(c, label)])}"
    if a == "reindent":
        return f"{fix['char']} on line {l}"
    return ""


def render(finding: Dict[str, Any], source: str, whole_file: bool) -> str:
    """Turn a structured finding into the human/model-facing paragraph."""
    k = finding["kind"]
    src_line = (lambda ln: _line_of(source, ln)) if whole_file else (lambda ln: source)

    if k == "mismatch":
        c, (cl, cc) = finding["closer"], finding["closer_pos"]
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        same = ol == cl
        where_open = f"col {oc + 1}" if same else f"line {ol} col {oc + 1}"
        head = (f"line {cl}: `{o}` opened at {where_open} is never closed — the `{c}` at col {cc + 1} "
                f"closes the outer {_NAMES[c]} while that {_NAMES[o]} is still open.")
        marks = [(cc, f"`{c}` arrives here, `{o}` still open")]
        if same:
            body = _mark(src_line(cl), [(oc, "opened here")] + marks)
        else:
            body = (_mark(src_line(ol), [(oc, f"`{o}` opened here (line {ol})")]) + "\n"
                    + _mark(src_line(cl), marks))
        return f"{head}\n{body}\n    Fix: add the missing `{_OPENERS[o]}` before the `{c}` (or delete a surplus `{o}`)."

    if k == "never_closed":
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        head = f"line {ol}: `{o}` opened at col {oc + 1} has no matching `{_OPENERS[o]}` anywhere after it."
        body = _mark(src_line(ol), [(oc, "opened here — never closed")])
        extra = ""
        if finding.get("also_open"):
            extra = "\n    Also still open at end of input: " + ", ".join(
                f"`{u[0]}` (line {u[1]} col {u[2] + 1})" for u in finding["also_open"])
        return f"{head}\n{body}\n    Fix: add the missing `{_OPENERS[o]}` where that expression should end.{extra}"

    if k == "extra":
        c, (cl, cc) = finding["closer"], finding["closer_pos"]
        head = f"line {cl}: `{c}` at col {cc + 1} has no opener to match — everything before it is already closed."
        body = _mark(src_line(cl), [(cc, "surplus closer")])
        return f"{head}\n{body}\n    Fix: delete this `{c}` (or add the `{_CLOSERS[c]}` it was meant to close)."

    if k == "eof_open":
        items = ", ".join(f"`{u[0]}` (line {u[1]} col {u[2] + 1})" for u in finding["open"])
        closers = "".join(_OPENERS[u[0]] for u in reversed(finding["open"]))
        return f"Still open at end of file: {items}.\n    Fix: close them in reverse order (`{closers}`)."

    if k == "tabs":
        prev = f" (line {finding['prev_line']} above is indented `{finding['expected']}`)" if finding.get("prev_line") else ""
        return (f"line {finding['line']}: indentation is `{finding['indent']}` — a mix of TAB (→) and space (·){prev}. "
                f"Python 3 refuses files that mix tabs and spaces in one block.\n"
                f"    Fix: re-indent this line with the same characters as its neighbours (spaces, normally).")

    if k == "unindent":
        lv = ", ".join(str(x) for x in finding["levels"]) or "0"
        return (f"line {finding['line']}: indented {finding['indent_len']} chars (`{finding['indent']}`), but the enclosing "
                f"blocks only use levels [{lv}]. A dedent must land exactly on an outer level.\n"
                f"    Fix: change this line's indent to one of those levels (check for a stray space or tab).")

    if k == "unexpected_indent":
        colon = ("" if finding.get("prev_ends_colon")
                 else f" Line {finding['prev_line']} does not end with `:`, so no new block is expected here.")
        return (f"line {finding['line']}: indented {finding['indent_len']} chars but the previous code line "
                f"({finding['prev_line']}) is at {finding['prev_indent_len']}.{colon}\n"
                f"    Fix: dedent this line to {finding['prev_indent_len']} — or, if a block was intended, add the missing `:` line above it.")

    if k == "open_at_error":
        o, (ol, oc) = finding["opener"], finding["opener_pos"]
        where = f"col {oc + 1}" if ol == finding["line"] else f"line {ol} col {oc + 1}"
        col_txt = f" col {finding['col'] + 1}" if finding.get("col") is not None else ""
        head = (f"line {finding['line']}: Python gave up at{col_txt} while the `{o}` opened at {where} was still open — "
                f"its `{_OPENERS[o]}` is missing before that point.")
        body = _mark(src_line(ol), [(oc, "opened here — still open where the error fired")])
        return f"{head}\n{body}"

    if k == "fstring_brace":
        col_txt = f" col {finding['col'] + 1}" if finding.get("col") is not None else ""
        if "single '}'" in finding["msg"]:
            head = (f"line {finding['line']}{col_txt}: a `}}` inside an f-string has no opening `{{` — either the `{{` "
                    f"is missing earlier in the string, or a literal brace was meant (write `}}}}`).")
        elif "expecting" in finding["msg"]:
            head = (f"line {finding['line']}{col_txt}: a `{{` inside an f-string opens a replacement field that is "
                    f"never closed — its `}}` is missing, or a literal brace was meant (write `{{{{`).")
        else:
            head = (f"line {finding['line']}{col_txt}: the `{{...}}` field of an f-string is broken here "
                    f"(Python: {finding['msg']}) — a `}}` is missing or misplaced inside the string.")
        return head

    if k == "expected_block":
        return (f"line {finding['stmt_line']} opens a `{finding['stmt']}` block (ends with `:`) but line {finding['line']} "
                f"is not indented deeper than it.\n"
                f"    Fix: indent the body under line {finding['stmt_line']}, or add `pass` if the body is intentionally empty.")
    return ""


def _static_hint(msg: str) -> Optional[str]:
    for needle, hint in _STATIC_HINTS:
        if needle in msg:
            return hint
    return None


# ---------------------------------------------------------------------------
# Locating the error and its source text
# ---------------------------------------------------------------------------

def _iter_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v, depth + 1)


def find_error(result: str) -> Optional[Dict[str, Any]]:
    """Find the first Python syntax error in a tool result string."""
    texts: List[str] = []
    try:
        parsed = json.loads(result)
        texts.extend(_iter_strings(parsed))
    except (ValueError, TypeError):
        texts.append(result)
    for text in texts:
        if "Error:" not in text:
            continue
        m = _TB_RE.search(text)
        if m:
            code = m.group("code") or ""
            col = None
            cm = _CARET_RE.search(text, m.start())
            if cm and cm.start() < m.end():
                # caret column relative to the echoed (dedented) line
                col = len(cm.group("pad")) - (len(code) - len(code.lstrip()))
                if col < 0:
                    col = None
            return {"file": m.group("file"), "line": int(m.group("line")), "col": col,
                    "code": code, "msg": m.group("msg").strip(),
                    "kind": m.group("kind"), "shape": "traceback"}
        m = _LINT_RE.search(text)
        if m:
            c = m.group("col")
            col = (int(c) - 1) if c and c.isdigit() and int(c) > 0 else None
            return {"file": None, "line": int(m.group("line")), "col": col, "code": "",
                    "msg": m.group("msg").strip(), "kind": m.group("kind"), "shape": "lint"}
    return None


_PY_SUFFIXES = (".py", ".pyw", ".pyi")


def _read_small_file(path: str) -> Optional[str]:
    """Read a Python source file named in a traceback — guarded.

    Tool output can contain attacker-controlled text (a `curl`, a web page in
    a log), and a crafted `File "...", line N ... SyntaxError:` could name any
    readable file. Two limits keep this from becoming a way to echo arbitrary
    file lines into the model's context: only Python-suffixed files are read,
    and (in source_for) only files that really fail to compile are used.
    """
    try:
        p = Path(path)
        if not p.is_absolute() or p.suffix.lower() not in _PY_SUFFIXES:
            return None
        if not p.is_file() or p.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def source_for(tool_name: str, args: Dict[str, Any], err: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Return (source, whole_file). whole_file=False → source is the echoed line only."""
    if tool_name == "write_file" and isinstance(args.get("content"), str):
        return args["content"], True
    if tool_name == "execute_code" and isinstance(args.get("code"), str):
        return args["code"], True
    if tool_name == "patch" and isinstance(args.get("path"), str):
        src = _read_small_file(os.path.expanduser(args["path"]))
        if src is not None and not _compiles(src):
            return src, True
    f = err.get("file")
    if f and not f.startswith("<"):
        src = _read_small_file(os.path.expanduser(f))
        if src is not None and not _compiles(src):   # a stale/forged traceback → ignore the file
            return src, True
    code = err.get("code") or ""
    if code.strip():
        # Python echoes the line with its indentation stripped; columns then
        # refer to the line as shown — which is exactly what the carets mark.
        return code.strip(), False
    return None, False


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def _disabled() -> bool:
    return os.environ.get("SYNTAX_ORACLE_DISABLE", "").strip().lower() in ("1", "true", "yes", "on")


_FOOTER_DELIM = (
    "This is a defect in the source text as written — not tool escaping, not shell quoting, "
    "not a linter false positive, not a broken Python. Fix the delimiter at the marked position; "
    "do not re-run unchanged, hexdump the file, or rewrite the line into a different shape without fixing it."
)
_FOOTER_INDENT = (
    "This is a whitespace defect in the source text as written — not tool escaping, not a broken Python. "
    "Fix the indentation of the reported line; do not re-run unchanged."
)
_FOOTER_HINT = (
    "This is a defect in the source text as written — not tool escaping, not shell quoting, "
    "not a broken Python. Fix the reported line; do not re-run unchanged."
)
_INDENT_KINDS = {"tabs", "unindent", "unexpected_indent", "expected_block"}


def build_verdict(tool_name: str, args: Any, result: Any) -> Optional[str]:
    """Pure function: return the verdict block or None. Exposed for tests."""
    if not isinstance(result, str) or not isinstance(args, dict):
        return None
    if "SyntaxError" not in result and "IndentationError" not in result and "TabError" not in result:
        return None
    err = find_error(result)
    if not err:
        return None
    msg = err["msg"]
    lines: List[str] = []
    footer = _FOOTER_HINT
    source, whole = source_for(tool_name, args, err)
    if source is not None:
        finding = diagnose(source, msg, err["line"], whole, err.get("col"))
        fix = repair(finding, source) if (finding and whole) else None
        if finding and finding.get("requires_fix") and not fix:
            finding = None
        if finding:
            bracket_cause = (finding["kind"] in _INDENT_KINDS and fix is not None
                             and fix["action"] in ("insert", "delete", "replace"))
            if bracket_cause:
                text = (f"line {finding['line']}: Python reports an indentation error here, but the real cause is a "
                        f"bracket problem on the line(s) above — this line is a continuation of a bracketed "
                        f"expression that was closed too early or never opened.")
                footer = _FOOTER_DELIM
            else:
                text = render(finding, source, whole)
                footer = _FOOTER_INDENT if finding["kind"] in _INDENT_KINDS else _FOOTER_DELIM
            if text:
                lines.append(text)
            if whole:
                if fix:
                    lines.append("✔ Verified one-edit fix (the whole file compiles after this): "
                                 + _render_fix(fix, source))
                else:
                    lines.append("No single-character edit within the search budget makes the file compile — "
                                 "the missing/surplus delimiter may sit inside a word, or more than one thing "
                                 "is wrong. Start from the position above.")
    hint = _static_hint(msg)
    if hint:
        lines.append(f"line {err['line']}: {hint}")
    if not lines:
        return None
    if tool_name in ("write_file", "patch"):
        lines.append("Note: the file WAS written with this error in it — patch the marked line.")
    body = "\n  ".join(lines)
    return f"🔎 syntax-oracle — deterministic diagnosis from Python's tokenizer, not a guess:\n  {body}\n  {footer}"


def _on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> Optional[str]:
    if _disabled():
        return None
    try:
        verdict = build_verdict(tool_name, args, result)
    except Exception as exc:  # noqa: BLE001 — never let diagnosis break a tool result
        logger.debug("syntax-oracle: diagnosis failed: %s", exc)
        return None
    if not verdict:
        return None
    return f"{result}\n\n{verdict}"


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
