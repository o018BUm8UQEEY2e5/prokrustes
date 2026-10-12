"""Test helper: compile a GBNF grammar back into a Python regular expression.

This exists so a generated grammar can be *checked* rather than trusted. The
grammar constrains generation, but nothing in the request fails loudly when the
compiler emits a rule that accepts the wrong language; by converting the
grammar back to a regex, a test can compare what it accepts against what
``re.fullmatch`` accepts for the original pattern.

Only the GBNF subset that ``prokrustes`` emits is handled: literals, character
classes, groups, alternation, repetition and rule references. Anything else
raises rather than guessing.
"""

from __future__ import annotations

import re

_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "[": "[", "]": "]",
}


def _escape_char(text: str, i: int) -> tuple[str, int]:
    code = text[i]
    i += 1
    if code in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[code], i
    if code == "x":
        return chr(int(text[i:i + 2], 16)), i + 2
    if code == "u":
        return chr(int(text[i:i + 4], 16)), i + 4
    if code == "U":
        return chr(int(text[i:i + 8], 16)), i + 8
    raise ValueError("unsupported escape \\%s" % code)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, end = 0, len(text)
    while i < end:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "#":
            while i < end and text[i] not in "\r\n":
                i += 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while True:
                if i >= end:
                    raise ValueError("unterminated string literal")
                if text[i] == '"':
                    i += 1
                    break
                if text[i] == "\\":
                    piece, i = _escape_char(text, i + 1)
                    buf.append(piece)
                else:
                    buf.append(text[i])
                    i += 1
            tokens.append(("str", "".join(buf)))
            continue
        if ch == "[":
            i += 1
            buf = []
            while True:
                if i >= end:
                    raise ValueError("unterminated char class")
                if text[i] == "]":
                    i += 1
                    break
                if text[i] == "\\":
                    piece, i = _escape_char(text, i + 1)
                    buf.append(piece)
                else:
                    buf.append(text[i])
                    i += 1
            tokens.append(("class", "".join(buf)))
            continue
        if text.startswith("::=", i):
            tokens.append(("op", "::="))
            i += 3
            continue
        if ch in "()|*+?{},.":
            tokens.append(("op", ch))
            i += 1
            continue
        match = re.match(r"[A-Za-z0-9_-]+", text[i:])
        if not match:
            raise ValueError("unexpected character %r" % ch)
        tokens.append(("name", match.group(0)))
        i += match.end()
    return tokens


def _class_to_regex(body: str) -> str:
    """Convert a GBNF character class to a Python one.

    GBNF gathers a class into a single alternation and negates the whole set,
    so ``[^\\n\\x00-\\x1F]`` means "neither newline nor a control character".
    Hex escapes become literal characters here because ``re`` understands the
    same escapes, keeping the result readable.
    """
    negated = body.startswith("^")
    if negated:
        body = body[1:]
    items: list[tuple] = []
    i = 0
    while i < len(body):
        low = body[i]
        i += 1
        if i < len(body) and body[i] == "-" and i + 1 < len(body):
            items.append((low, body[i + 1]))
            i += 2
        else:
            items.append((low, None))

    def cl(ch: str) -> str:
        return "\\" + ch if ch in "\\]^-[" else re.escape(ch)

    out = ["[^" if negated else "["]
    for low, high in items:
        out.append(cl(low) if high is None else "%s-%s" % (cl(low), cl(high)))
    out.append("]")
    return "".join(out)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def parse_alts(self):
        seqs = []
        cur: list = []
        while True:
            kind, val = self.peek()
            if kind is None or (kind == "op" and val in (")", "::=")):
                break
            if kind == "op" and val == "|":
                self.pos += 1
                seqs.append(cur)
                cur = []
                continue
            cur.append(self.parse_quant(self.parse_atom()))
        seqs.append(cur)
        return ("alt", seqs)

    def parse_atom(self):
        kind, val = self.tokens[self.pos]
        self.pos += 1
        if kind == "str":
            return ("str", val)
        if kind == "class":
            return ("class", val)
        if kind == "name":
            return ("ref", val)
        if kind == "op" and val == ".":
            return ("any",)
        if kind == "op" and val == "(":
            node = self.parse_alts()
            if self.peek()[1] != ")":
                raise ValueError("expected )")
            self.pos += 1
            return node
        raise ValueError("unexpected token %r" % (val,))

    def parse_quant(self, atom):
        kind, val = self.peek()
        if kind != "op":
            return atom
        if val == "*":
            self.pos += 1
            return ("rep", atom, 0, None)
        if val == "+":
            self.pos += 1
            return ("rep", atom, 1, None)
        if val == "?":
            self.pos += 1
            return ("rep", atom, 0, 1)
        if val == "{":
            self.pos += 1
            low = int(self.tokens[self.pos][1])
            self.pos += 1
            high = low
            if self.peek()[1] == ",":
                self.pos += 1
                if self.peek()[1] == "}":
                    high = None
                else:
                    high = int(self.tokens[self.pos][1])
                    self.pos += 1
            if self.peek()[1] != "}":
                raise ValueError("expected }")
            self.pos += 1
            return ("rep", atom, low, high)
        return atom


def _split_rules(text: str) -> dict[str, str]:
    rules: dict[str, str] = {}
    for line in text.splitlines():
        if "::=" not in line:
            continue
        name, _, body = line.partition("::=")
        rules[name.strip()] = body.strip()
    return rules


def _to_regex(node, rules, stack=()) -> str:
    kind = node[0]
    if kind == "str":
        return re.escape(node[1])
    if kind == "class":
        return _class_to_regex(node[1])
    if kind == "any":
        return "[\\s\\S]"
    if kind == "ref":
        name = node[1]
        if name in stack:
            raise ValueError("recursive rule %r" % name)
        if name not in rules:
            raise ValueError("undefined rule %r" % name)
        inner = _Parser(_tokenize(rules[name])).parse_alts()
        return "(?:" + _to_regex(inner, rules, stack + (name,)) + ")"
    if kind == "rep":
        base = _to_regex(node[1], rules, stack)
        low, high = node[2], node[3]
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
    if kind == "alt":
        body = "|".join(
            "".join(_to_regex(item, rules, stack) for item in seq)
            for seq in node[1]
        )
        return "(?:" + body + ")"
    raise ValueError("unknown node %r" % (node,))


def regex_for(grammar_text: str) -> re.Pattern:
    """Compile generated GBNF into an anchored Python regex."""
    rules = _split_rules(grammar_text)
    if "root" not in rules:
        raise ValueError("no root rule")
    node = _Parser(_tokenize(rules["root"])).parse_alts()
    return re.compile("\\A(?:" + _to_regex(node, rules) + ")\\Z", re.DOTALL)
