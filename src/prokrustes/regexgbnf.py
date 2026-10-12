"""Compile a Python regular expression into a GBNF expression.

Why this is possible: GBNF describes regular languages and so does ``re``, so
any pattern built from literals, classes, alternation, grouping and repetition
maps onto a rule. The cases that do not map are the ones where ``re`` reaches
*past* regular languages -- backreferences, lookahead, lookbehind -- because
they can express "matches something it already matched", which a finite
automaton cannot. Those are rejected rather than mistranslated, since a
silently-wrong grammar is worse than a refusal.

A translated grammar is always a subset of what ``re`` accepts, never a
superset: an unsupported construct raises instead of quietly widening or
narrowing the accepted language. That is why ``i`` (IGNORECASE) is expanded over
ASCII rather than approximated -- under Unicode, ``re`` also folds cases like
'K' (U+212A) to 'k', which no character class can express.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gbnf import GrammarError, check_syntax, gbnf_literal

# Ceilings. GBNF expands ``x{m,n}`` into ``n`` rules, and llama.cpp refuses a
# grammar where the running product of rule counts reaches 2000
# (MAX_REPETITION_THRESHOLD in llama-grammar.cpp), so unbounded-looking but
# enormous bounds are rejected up front rather than as an opaque server error.
MAX_EXPR_LEN = 3000
MAX_REPEAT = 1000

SUPPORTED_FLAGS = "is"

_FLAG_ERRORS = {
    "m": "MULTILINE is implied: anchors are only allowed at the pattern edges",
    "x": "VERBOSE mode is not supported",
    "a": "ASCII mode is not supported",
    "u": "UNICODE mode is not supported",
    "L": "LOCALE mode is not supported",
    "U": "UNGREEDY is not supported",
}


class RegexSyntaxError(Exception):
    """Raised for a pattern that cannot be expressed in GBNF."""


_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "a": "\a",
}

# A set node is (negated, [(lo, hi), ...]) over code points.
_SETS: dict[str, tuple[bool, list[tuple[int, int]]]] = {
    "d": (False, [(0x30, 0x39)]),
    "D": (True, [(0x30, 0x39)]),
    "w": (False, [(0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)]),
    "W": (True, [(0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)]),
    "s": (False, [(0x09, 0x0D), (0x20, 0x20)]),
    "S": (True, [(0x09, 0x0D), (0x20, 0x20)]),
}

# Not representable: they assert about position or earlier text.
_UNSUPPORTED_ESCAPES = {
    "1": "backreferences are not regular",
    "2": "backreferences are not regular",
    "3": "backreferences are not regular",
    "4": "backreferences are not regular",
    "5": "backreferences are not regular",
    "6": "backreferences are not regular",
    "7": "backreferences are not regular",
    "8": "backreferences are not regular",
    "9": "backreferences are not regular",
    "A": "\\A (string-start anchor) is implied by fullmatch",
    "Z": "\\Z (string-end anchor) is implied by fullmatch",
    "b": "\\b (word boundary) needs surrounding context",
    "B": "\\B (non-boundary) needs surrounding context",
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass
class _Ctx:
    pattern: str
    dotall: bool = False
    ignorecase: bool = False


def _fail(ctx: _Ctx, index: int, message: str):
    raise RegexSyntaxError("%s (at offset %d in %r)" % (message, index, ctx.pattern))


def _parse_alt(ctx: _Ctx, i: int):
    branches = []
    node, i = _parse_cat(ctx, i)
    branches.append(node)
    while i < len(ctx.pattern) and ctx.pattern[i] == "|":
        node, i = _parse_cat(ctx, i + 1)
        branches.append(node)
    if len(branches) == 1:
        return branches[0], i
    return ("alt", branches), i


def _parse_cat(ctx: _Ctx, i: int):
    items = []
    while i < len(ctx.pattern) and ctx.pattern[i] not in "|)":
        atom, i = _parse_atom(ctx, i)
        atom, i = _parse_quant(ctx, atom, i)
        items.append(atom)
    if len(items) == 1:
        return items[0], i
    return ("cat", items), i


def _parse_quant(ctx: _Ctx, atom, i: int):
    if i >= len(ctx.pattern):
        return atom, i
    ch = ctx.pattern[i]
    if ch == "*":
        low, high = 0, None
        i += 1
    elif ch == "+":
        low, high = 1, None
        i += 1
    elif ch == "?":
        low, high = 0, 1
        i += 1
    elif ch == "{":
        match = re.compile(r"\{(\d*)(,?)(\d*)\}").match(ctx.pattern, i)
        if not match or (not match.group(1) and not match.group(3)):
            return atom, i
        low = int(match.group(1) or 0)
        if match.group(2):
            high = int(match.group(3)) if match.group(3) else None
        else:
            high = low
        if high is not None and high < low:
            _fail(ctx, i, "repetition {%d,%d} is empty" % (low, high))
        i = match.end()
    else:
        return atom, i

    if low > MAX_REPEAT or (high is not None and high > MAX_REPEAT):
        _fail(
            ctx, i,
            "repetition bound above %d is too large to express in GBNF" % MAX_REPEAT,
        )

    if i < len(ctx.pattern) and ctx.pattern[i] in "?+":
        # Lazy ('*?') and possessive ('*+') change match priority, not the
        # accepted language, so both are safe to drop.
        i += 1
    return ("rep", atom, low, high), i


def _parse_atom(ctx: _Ctx, i: int):
    ch = ctx.pattern[i]
    start = i

    if ch == "(":
        return _parse_group(ctx, i)
    if ch == "[":
        return _parse_class(ctx, i)
    if ch == ".":
        # GBNF '.' is any character including newline; Python's is not.
        return (("any",) if ctx.dotall else ("set", True, [(0x0A, 0x0A)])), i + 1
    if ch == "^":
        if start != 0:
            _fail(ctx, i, "'^' is only allowed at the start of the pattern")
        return ("empty",), i + 1
    if ch == "$":
        if i != len(ctx.pattern) - 1:
            _fail(ctx, i, "'$' is only allowed at the end of the pattern")
        return ("empty",), i + 1
    if ch in "*+?":
        _fail(ctx, i, "nothing to repeat before %r" % ch)
    if ch == ")":
        _fail(ctx, i, "unbalanced ')'")
    if ch == "\\":
        return _parse_escape(ctx, i)

    node, i = _parse_char(ctx, i)
    if ctx.ignorecase:
        node = _ignorecase_literal(node)
    return node, i


def _parse_char(ctx: _Ctx, i: int):
    ch = ctx.pattern[i]
    if 0xD800 <= ord(ch) <= 0xDFFF:
        _fail(ctx, i, "lone surrogate is not a valid character")
    return ("lit", ch), i + 1


def _parse_group(ctx: _Ctx, i: int):
    rest = ctx.pattern[i:]
    if rest.startswith("(?:"):
        inner_at = i + 3
    elif rest.startswith("(?P<"):
        close = ctx.pattern.find(">", i)
        if close < 0:
            _fail(ctx, i, "unterminated group name")
        inner_at = close + 1
    elif rest.startswith("(?=") or rest.startswith("(?!"):
        _fail(ctx, i, "lookahead is not regular")
    elif rest.startswith("(?<=") or rest.startswith("(?<!"):
        _fail(ctx, i, "lookbehind is not regular")
    elif rest.startswith("(?>"):
        _fail(ctx, i, "atomic groups are not supported")
    elif rest.startswith("(?"):
        _fail(ctx, i, "inline flags and conditional groups are not supported")
    else:
        inner_at = i + 1

    node, j = _parse_alt(ctx, inner_at)
    if j >= len(ctx.pattern) or ctx.pattern[j] != ")":
        _fail(ctx, i, "missing ')'")
    return node, j + 1


def _parse_escape(ctx: _Ctx, i: int):
    if i + 1 >= len(ctx.pattern):
        _fail(ctx, i, "pattern ends with a backslash")
    code = ctx.pattern[i + 1]

    if code in _SETS:
        negated, ranges = _SETS[code]
        node = ("set", negated, list(ranges))
        if ctx.ignorecase:
            node = _ignorecase_set(node)
        return node, i + 2

    if code in _UNSUPPORTED_ESCAPES:
        _fail(ctx, i, _UNSUPPORTED_ESCAPES[code])

    if code == "x":
        width, start = 2, i + 2
    elif code == "u":
        width, start = 4, i + 2
    elif code == "U":
        width, start = 8, i + 2
    else:
        width = 0
        start = i + 1

    if width:
        digits = ctx.pattern[start:start + width]
        if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
            _fail(ctx, i, "incomplete \\%s escape" % code)
        node = ("lit", chr(int(digits, 16)))
        if ctx.ignorecase:
            node = _ignorecase_literal(node)
        return node, start + width

    if code in _SIMPLE_ESCAPES:
        node = ("lit", _SIMPLE_ESCAPES[code])
    elif code == "0":
        node = ("lit", "\0")
    elif code.isalnum():
        _fail(ctx, i, "unsupported escape \\%s" % code)
    else:
        node = ("lit", code)

    if ctx.ignorecase:
        node = _ignorecase_literal(node)
    return node, i + 2


def _parse_class(ctx: _Ctx, i: int):
    start = i
    j = i + 1
    negated = False
    if j < len(ctx.pattern) and ctx.pattern[j] == "^":
        negated = True
        j += 1

    ranges: list[tuple[int, int]] = []
    first = True
    while True:
        if j >= len(ctx.pattern):
            _fail(ctx, start, "unterminated character class")
        ch = ctx.pattern[j]
        if ch == "]" and not first:
            break
        first = False
        if ctx.pattern[j:j + 2] == "[:":
            _fail(ctx, j, "POSIX character classes are not supported")

        low, j, low_is_set = _class_item(ctx, j)
        if (
            not low_is_set
            and j < len(ctx.pattern)
            and ctx.pattern[j] == "-"
            and j + 1 < len(ctx.pattern)
            and ctx.pattern[j + 1] != "]"
        ):
            high, j, high_is_set = _class_item(ctx, j + 1)
            if high_is_set:
                _fail(ctx, j, "a character range cannot end in a shorthand class")
            if ord(high) < ord(low):
                _fail(ctx, j, "character range is reversed")
            ranges.append((ord(low), ord(high)))
        elif low_is_set:
            ranges.extend(low)
        else:
            ranges.append((ord(low), ord(low)))

    j += 1  # step over the closing ']'
    node = ("set", negated, _merge(ranges))
    if ctx.ignorecase:
        node = _ignorecase_set(node)
    return node, j


def _class_item(ctx: _Ctx, j: int):
    """Read one class element: a character, or the ranges of a shorthand.

    Returns ``(char, next_index, is_shorthand)``. When ``is_shorthand`` is set,
    ``char`` holds the shorthand's ranges instead of a single character, since
    a shorthand cannot serve as one end of a range.
    """
    if ctx.pattern[j] != "\\":
        return ctx.pattern[j], j + 1, False
    if j + 1 >= len(ctx.pattern):
        _fail(ctx, j, "class ends with a backslash")
    code = ctx.pattern[j + 1]
    if code in _SETS:
        negated, ranges = _SETS[code]
        if negated:
            raise RegexSyntaxError(
                "negated shorthand \\%s is not supported inside a character class"
                % code
            )
        return ranges, j + 2, True
    if code in _UNSUPPORTED_ESCAPES:
        _fail(ctx, j, _UNSUPPORTED_ESCAPES[code])
    node, nxt = _parse_escape(ctx, j)
    if node[0] != "lit":
        raise RegexSyntaxError("unsupported class escape at offset %d" % j)
    return node[1], nxt, False


# --------------------------------------------------------------------------
# IGNORECASE expansion (ASCII)
# --------------------------------------------------------------------------


def _ascii_fold(text: str) -> list[str]:
    out = {text}
    lower = text.lower()
    upper = text.upper()
    if len(lower) == 1 and lower != text:
        out.add(lower)
    if len(upper) == 1 and upper != text:
        out.add(upper)
    return sorted(out)


def _ignorecase_literal(node):
    if node[0] != "lit":
        return node
    chars = _ascii_fold(node[1])
    if len(chars) == 1:
        return node
    return ("set", False, [(ord(c), ord(c)) for c in chars])


def _ignorecase_set(node):
    _, negated, ranges = node
    if negated:
        # Complement first, then fold: folding a complement would need the
        # case pairs of everything outside the set.
        return ("set", True, _fold_ranges(_complement(ranges)))
    return ("set", negated, _fold_ranges(ranges))


def _fold_ranges(ranges):
    out = []
    for lo, hi in ranges:
        if hi - lo > 256:
            raise RegexSyntaxError(
                "cannot expand IGNORECASE over the range %d-%d" % (lo, hi)
            )
        for code in range(lo, hi + 1):
            for ch in _ascii_fold(chr(code)):
                out.append((ord(ch), ord(ch)))
    return _merge(out)


# --------------------------------------------------------------------------
# Range arithmetic
# --------------------------------------------------------------------------


def _merge(ranges):
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [list(ranges[0])]
    for lo, hi in ranges[1:]:
        if lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _complement(ranges):
    out = []
    cursor = 0
    for lo, hi in _merge(ranges):
        if lo > cursor:
            out.append((cursor, lo - 1))
        cursor = max(cursor, hi + 1)
    if cursor <= 0x10FFFF:
        out.append((cursor, 0x10FFFF))
    return out


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------

def _class_gbnf(negated: bool, ranges) -> str:
    pieces = []
    for lo, hi in ranges:
        if lo == hi:
            pieces.append(_class_char(chr(lo)))
        else:
            pieces.append(_class_char(chr(lo)) + "-" + _class_char(chr(hi)))
    if not pieces:
        raise RegexSyntaxError(
            "a character class that matches nothing has no GBNF equivalent"
        )
    return "[" + ("^" if negated else "") + "".join(pieces) + "]"


def _class_char(ch: str) -> str:
    """Escape one character for use inside a GBNF character class.

    GBNF's escape set is narrow (\\\\x, \\\\u, \\\\U, \\\\t, \\\\r, \\\\n, \\\\\\\\, \\\\",
    \\\\[, \\\\]) and has no \\\\- , yet inside a class a bare ``-`` is a range
    operator. A literal hyphen is therefore written ``\\x2d``, which parse_char
    accepts, rather than relying on it being last in the class.
    """
    if ch in "-^":
        # Inside a class, '-' starts a range and '^' negates only in first
        # position. GBNF has no escape for either, so both go out as hex.
        return "\\x%02x" % ord(ch)
    if ch in "[]\\":
        return "\\" + ch
    code = ord(ch)
    if code < 0x20 or code == 0x7F:
        if code <= 0xFF:
            return "\\x%02x" % code
        return "\\u%04x" % code
    return ch


def _emit(node) -> str:
    """Render a node so it is safe to use as one element of a sequence."""
    kind = node[0]
    if kind == "lit":
        return gbnf_literal(node[1])
    if kind == "empty":
        return '""'
    if kind == "set":
        return _class_gbnf(node[1], node[2])
    if kind == "any":
        return "."
    if kind == "cat":
        return " ".join(_emit(part) for part in node[1])
    if kind == "alt":
        return "(" + " | ".join(_emit(part) for part in node[1]) + ")"
    if kind == "rep":
        _, inner, low, high = node
        base = _emit(inner)
        if inner[0] in ("cat", "alt"):
            # A repeated sequence or alternation must be grouped, else the
            # quantifier would bind only to its final element.
            base = "(" + base + ")"
        if low == 0 and high is None:
            return base + "*"
        if low == 1 and high is None:
            return base + "+"
        if low == 0 and high == 1:
            return base + "?"
        if high is None:
            return base + "{%d,}" % low
        if low == high:
            return base + "{%d}" % low
        return base + "{%d,%d}" % (low, high)
    raise RegexSyntaxError("internal: unknown node %r" % (kind,))


def compile_pattern(pattern: str, flags: str = "") -> str:
    """Compile ``pattern`` to a GBNF expression, or raise RegexSyntaxError."""
    flag_bits = 0
    for flag in flags:
        if flag not in SUPPORTED_FLAGS:
            raise RegexSyntaxError(
                _FLAG_ERRORS.get(
                    flag, "unsupported flag %r (supported: %s)"
                    % (flag, ", ".join(SUPPORTED_FLAGS))
                )
            )
        flag_bits |= re.IGNORECASE if flag == "i" else re.DOTALL

    # Validate with re before translating, so a malformed pattern is reported
    # here rather than by our own parser. Nested-set warnings are expected:
    # they concern patterns we reject on other grounds anyway.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        try:
            re.compile(pattern, flag_bits)
        except re.error as exc:
            raise RegexSyntaxError("invalid pattern: %s" % exc) from None

    ctx = _Ctx(pattern=pattern, dotall="s" in flags, ignorecase="i" in flags)
    tree, end = _parse_alt(ctx, 0)
    if end != len(pattern):
        _fail(ctx, end, "unexpected %r" % pattern[end])

    expr = _emit(tree)
    if len(expr) > MAX_EXPR_LEN:
        raise RegexSyntaxError(
            "compiles to %d characters of GBNF, above the %d limit"
            % (len(expr), MAX_EXPR_LEN)
        )
    check_syntax("root ::= %s\n" % expr)
    return expr



