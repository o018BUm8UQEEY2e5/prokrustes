"""Unit tests for prokrustes.

The grammar is compiled from each placeholder's regex and then compiled *back*
into a Python regex here (see :func:`regex_for`), so a test can compare what
the grammar accepts against what ``re`` accepts. That catches generation bugs
without needing a model or a running server.

Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from prokrustes import (
    build_prompt,
    compile_template,
    extract,
    rebuild,
    validate,
)
from prokrustes import gbnf
from prokrustes.gbnf import Grammar, GrammarError, gbnf_literal, rule_name
from prokrustes.template import Field, TemplateError, parse_field, render, scan
from test_regexgbnf import regex_for

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)

# --------------------------------------------------------------------------
# A GBNF -> regex compiler, used only to check generated grammars.
# --------------------------------------------------------------------------


def _escape_char(text: str, i: int) -> tuple[str, int]:
    code = text[i]
    i += 1
    simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "[": "[", "]": "]"}
    if code in simple:
        return simple[code], i
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

    def parse_alts(self, rules):
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
            cur.append(self.parse_quant(self.parse_atom(rules)))
        seqs.append(cur)
        return ("alt", seqs)

    def parse_atom(self, rules):
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
            node = self.parse_alts(rules)
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
        inner = _Parser(_tokenize(rules[name])).parse_alts(rules)
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
            "".join(_to_regex(item, rules, stack) for item in seq) for seq in node[1]
        )
        return "(?:" + body + ")"
    raise ValueError("unknown node %r" % (node,))


def regex_for(grammar_text: str) -> re.Pattern:
    """Compile a generated grammar back into an anchored Python regex."""
    rules = _split_rules(grammar_text)
    if "root" not in rules:
        raise ValueError("no root rule")
    node = _Parser(_tokenize(rules["root"])).parse_alts(rules)
    return re.compile("\\A(?:" + _to_regex(node, rules) + ")\\Z", re.DOTALL)


def grammar_for(pattern: str, flags: str = "") -> str:
    """Compile one pattern into a complete one-placeholder grammar."""
    compiled = compile_template("{%s:/%s/%s}" % ("v", pattern, flags),
                                use_prefill=False)
    return compiled.grammar


def accepted_by_grammar(grammar: str, text: str) -> bool:
    return bool(regex_for(grammar).match(text))


# --------------------------------------------------------------------------
# Placeholder parsing
# --------------------------------------------------------------------------


class TestParseField(unittest.TestCase):
    def test_simple(self):
        field = parse_field("port:/[0-9]+/")
        self.assertEqual(field.name, "port")
        self.assertEqual(field.pattern, "[0-9]+")
        self.assertEqual(field.flags, "")
        self.assertFalse(field.anonymous)
        self.assertEqual(field.spec_text, "port:/[0-9]+/")

    def test_flags(self):
        field = parse_field("g:/hello/i")
        self.assertEqual(field.pattern, "hello")
        self.assertEqual(field.flags, "i")

    def test_both_flags(self):
        field = parse_field("g:/a.b/is")
        self.assertEqual(field.flags, "is")

    def test_pattern_with_slashes_must_escape(self):
        field = parse_field(r"u:/[a-z]+\/[a-z]+/")
        self.assertEqual(field.pattern, r"[a-z]+\/[a-z]+")
        self.assertTrue(re.fullmatch(field.pattern, "ab/cd"))

    def test_pattern_with_braces(self):
        field = parse_field("v:/a{2,3}/")
        self.assertEqual(field.pattern, "a{2,3}")

    def test_errors(self):
        cases = {
            "port": "has no pattern",
            "port:int": "must specify a regex",
            ":x": "invalid field name",
            "1bad:/x/": "invalid field name",
            "port:/x/g": "unsupported flag",
            "port:/": "no closing",
            "port:/x": "no closing",
            "port://": "empty pattern",
        }
        for body, hint in cases.items():
            with self.subTest(body=body), self.assertRaises(TemplateError) as ctx:
                parse_field(body)
            self.assertIn(hint, str(ctx.exception))

    def test_unsupported_constructs_reported(self):
        for body, hint in [
            ("v:/(a)\\1/", "backreference"),
            ("v:/(?=x)/", "lookahead"),
            ("v:/\\bword/", "word boundary"),
            ("v:/(?i)abc/", "inline flags"),
            ("v:/[[:alpha:]]/", "POSIX"),
            ("v:/a{1,5000}/", "too large"),
        ]:
            with self.subTest(body=body), self.assertRaises(TemplateError) as ctx:
                parse_field(body)
            self.assertIn(hint, str(ctx.exception))

    def test_bare_slash_message_is_specific(self):
        with self.assertRaises(TemplateError) as ctx:
            parse_field("u:/https?://[a-z]+/")
        self.assertIn("unescaped '/'", str(ctx.exception))


class TestAnonymousFields(unittest.TestCase):
    """The name is optional; an unnamed placeholder needs no identity."""

    def test_parsed(self):
        field = parse_field("/[0-9]+/")
        self.assertTrue(field.anonymous)
        self.assertEqual(field.pattern, "[0-9]+")
        self.assertEqual(field.spec_text, "/[0-9]+/")

    def test_positional_id(self):
        parts = scan("a{/x/} b{/y/} c{z:/z/}")
        self.assertEqual([p.name for p in parts if isinstance(p, Field)],
                         ["field1", "field2", "z"])
        self.assertEqual([p.anonymous for p in parts if isinstance(p, Field)],
                         [True, True, False])

    def test_grammar_uses_positional_rules(self):
        compiled = compile_template("a={/x/} b={/y/}", use_prefill=False)
        self.assertIn("v-field1 ::=", compiled.grammar)
        self.assertIn("v-field2 ::=", compiled.grammar)
        self.assertIn('root ::= "a=" v-field1 " b=" v-field2', compiled.grammar)

    def test_spec_text_variants(self):
        named = parse_field("port:/[0-9]+/")
        self.assertEqual(named.spec_text, "port:/[0-9]+/")
        self.assertEqual(named.pattern_text, "/[0-9]+/")
        anonymous = parse_field("/[0-9]+/")
        self.assertEqual(anonymous.spec_text, "/[0-9]+/")
        self.assertEqual(anonymous.pattern_text, "/[0-9]+/")

    def test_render_roundtrips_both_forms(self):
        source = "a={port:/[0-9]+/} b={/[a-z]+/}"
        self.assertEqual(render(scan(source)), source)

    def test_anonymous_excluded_from_values(self):
        compiled = compile_template("a={/x/} b={named:/y/}", use_prefill=False)
        raws, _, _ = extract(compiled.parts, "a=x b=y")
        values, problems = validate(compiled.parts, raws)
        self.assertEqual(problems, [])
        self.assertEqual(values, {"named": "y"})

    def test_still_validated(self):
        compiled = compile_template("a={/[a-z]+/}", use_prefill=False)
        _, problems = validate(compiled.parts, {"field1": "123"})
        self.assertEqual(len(problems), 1)

    def test_omitted_from_prompt_list(self):
        compiled = compile_template("a={/x/} b={named:/y/}", use_prefill=False)
        prompt = build_prompt(compiled)
        self.assertIn("  named:", prompt)
        self.assertNotIn("field1:", prompt)
        # The pattern still reaches the model, inline in the template.
        self.assertIn("{/x/}", prompt)

    def test_cannot_be_pinned(self):
        with self.assertRaises(TemplateError) as ctx:
            compile_template("a={/x/}", pins={"field1": "v"})
        self.assertIn("anonymous", str(ctx.exception))

    def test_two_of_a_kind_need_no_names(self):
        compiled = compile_template(
            "listen {/[1-9][0-9]{0,4}/};\nlisten {/[1-9][0-9]{0,4}/};",
            use_prefill=False,
        )
        self.assertIn("v-field1", compiled.grammar)
        self.assertIn("v-field2", compiled.grammar)

    def test_explain_marks_anonymous(self):
        compiled = compile_template("a={/x/}", use_prefill=False)
        self.assertEqual(compiled.unbounded, [])

    def test_rule_names_recorded_for_explain(self):
        compiled = compile_template("a={port:/[0-9]+/} b={port:/[0-9]+/}",
                                    use_prefill=False)
        fields = [p for p in compiled.parts if isinstance(p, Field)]
        self.assertEqual(len(fields), 2)
        for part in fields:
            self.assertTrue(part.rule)
            self.assertIn("%s ::=" % part.rule, compiled.grammar)
        # A repeated name shares one rule in both places.
        self.assertEqual(fields[0].rule, fields[1].rule)


class TestScan(unittest.TestCase):
    def test_literal_only(self):
        self.assertEqual(scan("hello"), ["hello"])

    def test_field(self):
        parts = scan("a{n:/x/}b")
        self.assertEqual(parts[0], "a")
        self.assertIsInstance(parts[1], Field)
        self.assertEqual(parts[1].name, "n")
        self.assertEqual(parts[2], "b")

    def test_brace_escapes(self):
        self.assertEqual(scan("{{x}}"), ["{x}"])
        self.assertEqual(scan("{{}}"), ["{}"])
        self.assertEqual(scan("a{{b"), ["a{b"])

    def test_json_shape(self):
        parts = scan('{{"k": "{v:/[a-z]+/}"}}')
        self.assertEqual(parts[0], '{"k": "')
        self.assertIsInstance(parts[1], Field)
        self.assertEqual(parts[2], '"}')

    def test_adjacent_placeholders(self):
        parts = scan("{a:/x/}-{b:/y/}")
        self.assertEqual(len(parts), 3)

    def test_render_roundtrip(self):
        source = '{{"k": "{v:/[a-z]+/}"}}'
        self.assertEqual(render(scan(source)), source)

    def test_errors(self):
        for text in ["{unclosed", "a } b", "{{a}", "{a:/x/"]:
            with self.subTest(text=text):
                with self.assertRaises(TemplateError):
                    scan(text)


# --------------------------------------------------------------------------
# Grammar generation
# --------------------------------------------------------------------------


class TestGrammar(unittest.TestCase):
    def test_single_placeholder(self):
        compiled = compile_template("{v:/[a-z]+/}", use_prefill=False)
        self.assertIn("v-v ::=", compiled.grammar)
        self.assertIn("root ::= v-v", compiled.grammar)

    def test_literal_between(self):
        compiled = compile_template("a={v:/x/};", use_prefill=False)
        self.assertIn('root ::= "a=" v-v ";"', compiled.grammar)

    def test_pattern_shapes_accepted(self):
        for good in ["8080", "api-gw", "2026-07-04", "a,b,c"]:
            with self.subTest(value=good):
                grammar = grammar_for(r"[0-9a-z,-]+")
                self.assertTrue(accepted_by_grammar(grammar, good))

    def test_pattern_shapes_rejected(self):
        grammar = grammar_for(r"[0-9]+")
        for bad in ["", "a", "8080a", "80 80"]:
            with self.subTest(value=bad):
                self.assertFalse(accepted_by_grammar(grammar, bad))

    def test_rule_name_underscore_becomes_dash(self):
        compiled = compile_template("{api_key:/x/}", use_prefill=False)
        self.assertIn("v-api-key ::=", compiled.grammar)

    def test_rule_names_are_unique(self):
        compiled = compile_template("{x:/a/}-{x:/a/}", use_prefill=False)
        self.assertEqual(compiled.grammar.count("v-x ::="), 1)

    def test_repeated_name_shares_one_rule(self):
        compiled = compile_template("a={n:/x/} b={n:/x/}", use_prefill=False)
        self.assertEqual(compiled.grammar.count("v-n ::="), 1)

    def test_repeated_name_requires_identical_pattern(self):
        with self.assertRaises(TemplateError):
            compile_template("{a:/x/} {a:/y/}")

    def test_adjacent_fields_rejected(self):
        with self.assertRaises(TemplateError):
            compile_template("{a:/x/}{b:/y/}")

    def test_json_template_round_trip(self):
        compiled = compile_template('{{"k": "{v:/[a-z]+/}"}}', use_prefill=False)
        self.assertTrue(accepted_by_grammar(compiled.grammar, '{"k": "ab"}'))
        self.assertFalse(accepted_by_grammar(compiled.grammar, '{"k": "AB"}'))

    def test_every_example_compiles(self):
        folder = EXAMPLES
        found = sorted(f for f in os.listdir(folder) if f.endswith(".tpl"))
        self.assertGreaterEqual(len(found), 4)
        for name in found:
            with self.subTest(example=name):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    compiled = compile_template(handle.read())
                self.assertTrue(compiled.grammar)
                gbnf.check_syntax(compiled.grammar)


# --------------------------------------------------------------------------
# Literal escaping and rule names
# --------------------------------------------------------------------------


class TestUnboundedWarning(unittest.TestCase):
    """A `*` or `+` lets a small model run on and truncate the document.

    Observed in practice: `[a-z0-9]+` produced a slug thousands of characters
    long, so the file was cut off mid-value.
    """

    def test_detected(self):
        for pattern in [r"[a-z]+", r"a*", r"x[0-9]+y", r"(ab)+"]:
            with self.subTest(pattern=pattern):
                compiled = compile_template("{v:/%s/}" % pattern,
                                            use_prefill=False)
                self.assertEqual(compiled.unbounded, ["v"])

    def test_bounded_is_quiet(self):
        for pattern in [r"[a-z]{1,12}", r"a{2}", r"a{2,}", r"[0-9]{4}",
                        r"(ab){1,3}", r"x|y"]:
            with self.subTest(pattern=pattern):
                compiled = compile_template("{v:/%s/}" % pattern,
                                            use_prefill=False)
                self.assertEqual(compiled.unbounded, [])

    def test_metacharacters_in_classes_do_not_count(self):
        # '+' and '*' are ordinary members of a class; only a quantifier
        # outside one is unbounded.
        self.assertFalse(gbnf.has_unbounded_repetition("[a-z+*]"))
        self.assertFalse(gbnf.has_unbounded_repetition("[a-z+*]{2}"))
        self.assertTrue(gbnf.has_unbounded_repetition("[a-z+*]+"))
        self.assertTrue(gbnf.has_unbounded_repetition('"a+b" [0-9]*'))
        self.assertFalse(gbnf.has_unbounded_repetition('"+" "[0-9]{2}'))

    def test_examples_are_all_bounded(self):
        folder = EXAMPLES
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".tpl"):
                continue
            with self.subTest(example=name):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    compiled = compile_template(handle.read())
                self.assertEqual(
                    compiled.unbounded, [],
                    "%s has unbounded fields: %s" % (name, compiled.unbounded),
                )


class TestLiterals(unittest.TestCase):
    def test_roundtrip(self):
        for text in ["", "a", 'say "hi"', "back\\slash", "line\nbreak",
                     "tab\there", "# not a comment", "a-b", "{{}}", "\r\n",
                     'mixed "a\\b"\n#\t']:
            regex = regex_for("root ::= %s" % gbnf_literal(text))
            self.assertIsNotNone(regex.match(text), repr(text))

    def test_control_chars_escaped(self):
        self.assertEqual(gbnf_literal("\x01"), '"\\x01"')

    def test_not_a_prefix_match(self):
        regex = regex_for("root ::= %s" % gbnf_literal('a"'))
        self.assertIsNone(regex.match('a"b"'))


class TestRuleNames(unittest.TestCase):
    def test_shape(self):
        for name in ["api_key", "x", "9lives", "A-B", "__x"]:
            with self.subTest(name=name):
                generated = rule_name(name, set())
                self.assertRegex(generated, r"^[a-z][a-z0-9-]*$")

    def test_collisions(self):
        taken: set[str] = set()
        self.assertEqual(rule_name("x", taken), "v-x")
        self.assertEqual(rule_name("x", taken), "v-x-2")
        self.assertEqual(rule_name("x", taken), "v-x-3")


class TestSyntaxGuard(unittest.TestCase):
    def test_accepts_valid(self):
        gbnf.check_syntax('root ::= ("a" | "b") " (" [0-9]{2} ")"\n')

    def test_rejects_unopened_group(self):
        # The '(' before the quotes opens a group that the ')' inside the quoted
        # terminal never closes; llama-server reports this only as
        # "failed to parse grammar", so it is checked locally.
        with self.assertRaises(GrammarError):
            gbnf.check_syntax('root ::= ("[A-Z][a-z]{2})" " "\n')

    def test_rejects_close_after_literal(self):
        with self.assertRaises(GrammarError):
            gbnf.check_syntax('root ::= "a" ) "b"\n')

    def test_rejects_unterminated_string(self):
        with self.assertRaises(GrammarError):
            gbnf.check_syntax('root ::= "abc\n')

    def test_parens_inside_strings_ignored(self):
        gbnf.check_syntax('root ::= "(((" ")))"\n')

    def test_escaped_quote_does_not_end_the_literal(self):
        gbnf.check_syntax('root ::= "{\\"a\\": \\"b\\"}"\n')
        with self.assertRaises(GrammarError):
            gbnf.check_syntax('root ::= "{\\"a": "b")"\n')

    def test_escaped_backslash_then_quote_closes(self):
        gbnf.check_syntax('root ::= "a\\\\" "b"\n')


# --------------------------------------------------------------------------
# Extraction, rebuilding and validation
# --------------------------------------------------------------------------


class TestExtractValidate(unittest.TestCase):
    TEMPLATE = "listen {port:/[0-9]+/};\nhost {h:/[a-z.]+/};\n"

    def test_happy_path(self):
        compiled = compile_template(self.TEMPLATE, use_prefill=False)
        raws, problem, _ = extract(
            compiled.parts, "listen 8080;\nhost api.example.com;\n"
        )
        self.assertIsNone(problem)
        values, problems = validate(compiled.parts, raws)
        self.assertEqual(problems, [])
        self.assertEqual(values, {"port": "8080", "h": "api.example.com"})

    def test_strings_by_default(self):
        compiled = compile_template("{n:/[0-9]+/}", use_prefill=False)
        raws, _, _ = extract(compiled.parts, "42")
        values, _ = validate(compiled.parts, raws)
        self.assertEqual(values["n"], "42")
        self.assertIsInstance(values["n"], str)

    def test_value_failing_pattern_is_reported(self):
        compiled = compile_template("{n:/[a-z]+/}", use_prefill=False)
        _, problems = validate(compiled.parts, {"n": "ABC"})
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match", problems[0])

    def test_divergence_reported(self):
        compiled = compile_template(self.TEMPLATE, use_prefill=False)
        _, problem, _ = extract(compiled.parts, "listen 8080;\nWRONG\n")
        self.assertIsNotNone(problem)

    def test_truncation_reported(self):
        compiled = compile_template(self.TEMPLATE, use_prefill=False)
        _, problem, _ = extract(compiled.parts, "listen 8080;\nhost a")
        self.assertIn("unterminated", problem)

    def test_repeated_first_occurrence_wins(self):
        compiled = compile_template("a={n:/x/} b={n:/x/}", use_prefill=False)
        raws, problem, positions = extract(compiled.parts, "a=api b=web")
        self.assertIsNone(problem)
        self.assertEqual(raws, {"n": "api"})
        self.assertEqual(len(positions["n"]), 2)

    def test_repeated_is_rebuilt_consistently(self):
        compiled = compile_template("a={n:/x/} b={n:/x/}", use_prefill=False)
        raws, _, _ = extract(compiled.parts, "a=api b=web")
        fixed = rebuild(compiled.parts, raws)
        self.assertEqual(fixed, "a=api b=api")
        raws, problem, _ = extract(compiled.parts, fixed)
        self.assertIsNone(problem)

    def test_pins_extracted(self):
        compiled = compile_template("a={a:/fix/} b={b:/y/}",
                                    pins={"a": "fix"}, use_prefill=False)
        raws, problem, _ = extract(compiled.parts, "a=fix b=Y")
        self.assertIsNone(problem)
        self.assertEqual(raws["b"], "Y")


class TestPins(unittest.TestCase):
    def test_pin_validated(self):
        compile_template("{v:/[A-Z]{3}/}", pins={"v": "ABC"}, use_prefill=False)
        for bad in ["abc", "AB", "ABCD", ""]:
            with self.subTest(value=bad), self.assertRaises(TemplateError):
                compile_template("{v:/[A-Z]{3}/}", pins={"v": bad},
                                 use_prefill=False)

    def test_pin_removed_from_grammar(self):
        compiled = compile_template("{a:/x/}-{v:/pin/}", pins={"v": "pin"},
                                    use_prefill=False)
        self.assertNotIn("v-v ::=", compiled.grammar)
        self.assertIn('v-a "-pin"', compiled.grammar)

    def test_unknown_pin(self):
        with self.assertRaises(TemplateError):
            compile_template("{a:/x/}", pins={"zzz": "1"})

    def test_pin_honours_flags(self):
        compile_template("{v:/abc/i}", pins={"v": "ABC"}, use_prefill=False)
        with self.assertRaises(TemplateError):
            compile_template("{v:/abc/i}", pins={"v": "abd"})


# --------------------------------------------------------------------------
# Prompt and endpoint
# --------------------------------------------------------------------------


class TestPrompt(unittest.TestCase):
    def test_mentions_each_field_once(self):
        compiled = compile_template("a={n:/x/} b={m:/y/}", use_prefill=False)
        prompt = build_prompt(compiled)
        self.assertEqual(prompt.count("  n:"), 1)
        self.assertEqual(prompt.count("  m:"), 1)

    def test_repeated_field_says_so(self):
        compiled = compile_template("{n:/x/} and {n:/x/}", use_prefill=False)
        self.assertIn("same value is used at all 2 places", build_prompt(compiled))

    def test_pattern_shown(self):
        compiled = compile_template("{n:/[0-9]{4}-[0-9]{2}/}", use_prefill=False)
        prompt = build_prompt(compiled)
        self.assertIn("[0-9]{4}-[0-9]{2}", prompt)

    def test_known_patterns_get_a_description(self):
        cases = {
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}": "UUID",
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}": "ISO date",
            r"[a-z0-9]+(-[a-z0-9]+)*": "lowercase name",
            r"#[0-9a-fA-F]{6}": "colour",
        }
        from prokrustes.cli import _hint

        for pattern, word in cases.items():
            with self.subTest(pattern=pattern):
                self.assertIn(word, _hint(pattern, ""))

    def test_unknown_pattern_falls_back_to_regex(self):
        from prokrustes.cli import _hint

        self.assertIn("matching this regex", _hint(r"z{3,7}", ""))

    def test_template_omitted_from_prompt(self):
        compiled = compile_template("a={n:/x/} b={m:/y/}", use_prefill=False)
        prompt = build_prompt(compiled, include_template=False)
        self.assertNotIn("--- TEMPLATE ---", prompt)
        self.assertNotIn("a={n:/x/}", prompt)
        self.assertIn("  n:", prompt)
        self.assertIn("  m:", prompt)

    def test_custom_instructions_replace_preamble(self):
        compiled = compile_template("a={n:/x/}", use_prefill=False)
        prompt = build_prompt(compiled, instructions=["Do it.", "Be terse."])
        self.assertTrue(prompt.startswith("Do it.\nBe terse.\n"))
        self.assertNotIn("You are completing", prompt)
        self.assertIn("  n:", prompt)
        self.assertIn("--- TEMPLATE ---", prompt)
        self.assertIn("a={n:/x/}", prompt)
        compiled = compile_template("a={n:/x/} b={m:/y/}", use_prefill=False)
        prompt = build_prompt(compiled, include_template=False)
        self.assertNotIn("--- TEMPLATE ---", prompt)
        self.assertNotIn("a={n:/x/}", prompt)
        self.assertIn("  n:", prompt)
        self.assertIn("  m:", prompt)

    def test_anonymous_listed_when_template_omitted(self):
        compiled = compile_template("a={/x/} b={named:/y/}", use_prefill=False)
        self.assertNotIn("field1:", build_prompt(compiled))
        omitted = build_prompt(compiled, include_template=False)
        self.assertIn("field1:", omitted)
        self.assertIn("  named:", omitted)

    def test_no_prompt_returns_only_prefill(self):
        compiled = compile_template("listen {port:/[0-9]+/};")
        self.assertEqual(build_prompt(compiled, include_prompt=False),
                         compiled.prefill)
        bare = compile_template("{v:/[a-z]+/}", use_prefill=False)
        self.assertEqual(build_prompt(bare, include_prompt=False), "")

    def test_no_prompt_chat_message_is_empty(self):
        import prokrustes.cli as cli

        compiled = compile_template("a={v:/x/}", use_prefill=False)
        self.assertEqual(cli.build_chat_messages(compiled, include_prompt=False),
                         [{"role": "user", "content": ""}])


class TestEndpointConstruction(unittest.TestCase):
    """A missing /completion suffix is a 404, which is easy to reintroduce."""

    def _capture(self, base):
        import prokrustes.cli as prokrustes

        seen = {}

        class FakeResponse:
            def read(self):
                return b'{"content": "x", "stop_type": "eos"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["data"] = json.loads(req.data.decode())
            return FakeResponse()

        original = prokrustes.urllib.request.urlopen
        prokrustes.urllib.request.urlopen = fake_urlopen
        try:
            prokrustes.post_completion(base, {"prompt": "p"}, 5.0)
        finally:
            prokrustes.urllib.request.urlopen = original
        return seen

    def test_appends_completion(self):
        self.assertEqual(
            self._capture("http://127.0.0.1:8080")["url"],
            "http://127.0.0.1:8080/completion",
        )

    def test_tolerates_trailing_slash(self):
        self.assertEqual(
            self._capture("http://127.0.0.1:8080/")["url"],
            "http://127.0.0.1:8080/completion",
        )

    def test_tolerates_base_with_path(self):
        self.assertEqual(
            self._capture("http://host/llama")["url"],
            "http://host/llama/completion",
        )


class TestChatMode(unittest.TestCase):
    """--chat puts the chat wrapper in the prompt, not the grammar."""

    def _fill_with_fake_server(self, source, rendered, completion_body,
                               no_prompt=False, instructions=None):
        import prokrustes.cli as cli
        from argparse import Namespace

        calls = []
        queue = [
            {"prompt": rendered},
            {"content": completion_body, "stop_type": "eos"},
        ]

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            calls.append((req.full_url, json.loads(req.data.decode("utf-8"))))
            return FakeResponse(queue.pop(0))

        original = cli.urllib.request.urlopen
        cli.urllib.request.urlopen = fake_urlopen
        try:
            compiled = cli.compile_template(source)
            args = Namespace(chat=True, template_in_prompt=True,
                             no_prompt=no_prompt,
                             n_predict=16, temperature=0.0, top_k=1,
                             top_p=1.0, seed=7, dynatemp=0.0, stop=[],
                             timeout=5.0)
            content = cli.fill("http://127.0.0.1:8080/", compiled, args,
                               instructions)
        finally:
            cli.urllib.request.urlopen = original
        return compiled, calls, content

    def test_user_message_carries_our_prompt(self):
        import prokrustes.cli as cli

        compiled = cli.compile_template("{v:/[a-z]+/}", use_prefill=False)
        self.assertEqual(cli.build_chat_messages(compiled),
                         [{"role": "user", "content": cli.build_prompt(compiled)}])

    def test_chat_uses_apply_template_then_completion_with_document_grammar(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>...<|im_start|>assistant\n"
        compiled, calls, content = self._fill_with_fake_server(
            "listen {port:/[0-9]+/};", rendered, "8080;"
        )
        self.assertEqual(content, "8080;")
        self.assertEqual(calls[0][0], "http://127.0.0.1:8080/apply-template")
        self.assertEqual(calls[0][1],
                         {"messages": cli.build_chat_messages(compiled)})
        self.assertEqual(calls[1][0], "http://127.0.0.1:8080/completion")
        completion = calls[1][1]
        self.assertEqual(completion["prompt"], rendered + compiled.prefill)
        self.assertEqual(completion["grammar"], compiled.grammar)
        self.assertNotIn("messages", completion)

    def test_apply_template_must_return_prompt(self):
        import prokrustes.cli as cli

        class FakeResponse:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            return FakeResponse()

        original = cli.urllib.request.urlopen
        cli.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(SystemExit):
                cli.apply_chat_template("http://127.0.0.1:8080",
                                        [{"role": "user", "content": "x"}], 5.0)
        finally:
            cli.urllib.request.urlopen = original


    def test_chat_is_default_in_dry_run(self):
        code, output = self._dry_run([])
        self.assertEqual(code, 0)
        self.assertIn('"role": "user"', output)
        self.assertIn("--- CHAT TEMPLATE ---", output)

    def test_no_chat_restores_raw_dry_run(self):
        code, output = self._dry_run(["--no-chat"])
        self.assertEqual(code, 0)
        self.assertIn("You are completing a text template.", output)
        self.assertNotIn('"role": "user"', output)

    def test_no_template_in_prompt_dry_run(self):
        code, output = self._dry_run(["--no-template-in-prompt"])
        self.assertEqual(code, 0)
        self.assertNotIn("--- TEMPLATE ---", output)
        self.assertNotIn("a={v:/x/}", output)

    def test_no_prompt_sends_empty_user_message(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>...<|im_start|>assistant\n"
        compiled, calls, content = self._fill_with_fake_server(
            "listen {port:/[0-9]+/};", rendered, "8080;", no_prompt=True
        )
        self.assertEqual(content, "8080;")
        self.assertEqual(calls[0][1],
                         {"messages": [{"role": "user", "content": ""}]})
        completion = calls[1][1]
        self.assertEqual(completion["prompt"], rendered + compiled.prefill)
        self.assertEqual(completion["grammar"], compiled.grammar)

    def test_no_prompt_dry_run(self):
        code, output = self._dry_run(["--no-prompt"])
        self.assertEqual(code, 0)
        self.assertIn('"content": ""', output)

    def test_no_prompt_conflicts_with_template_in_prompt(self):
        import prokrustes.cli as cli

        for argv in (["--no-prompt", "--no-template-in-prompt"],
                     ["--no-prompt", "--template-in-prompt"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                cli.main(["-", "--dry-run"] + argv)

    def test_fill_with_custom_instructions(self):
        rendered = "<CHAT>...<|im_start|>assistant\n"
        _, calls, content = self._fill_with_fake_server(
            "a={v:/x/}", rendered, "a=x", instructions=["Do it."]
        )
        self.assertEqual(content, "a=x")
        user_content = calls[0][1]["messages"][0]["content"]
        self.assertTrue(user_content.startswith("Do it.\n"))
        self.assertIn("a={v:/x/}", user_content)

    def test_instructions_dry_run_reads_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                         delete=True) as handle:
            handle.write("Do it.\nBe terse.\n")
            handle.flush()
            code, output = self._dry_run(["--no-chat", "--instructions",
                                          handle.name])
        self.assertEqual(code, 0)
        self.assertIn("Do it.\nBe terse.\n", output)
        self.assertNotIn("You are completing", output)
        self.assertIn("--- TEMPLATE ---", output)

    def test_instructions_conflicts_with_no_prompt(self):
        import prokrustes.cli as cli

        with self.assertRaises(SystemExit):
            cli.main(["-", "--dry-run", "--no-prompt",
                      "--instructions", "whatever.txt"])

    def test_missing_instructions_file_errors(self):
        import prokrustes.cli as cli

        with self.assertRaises(SystemExit):
            cli.main(["-", "--dry-run", "--no-chat",
                      "--instructions", "/nonexistent/preamble.txt"])

    def test_template_in_prompt_is_default_dry_run(self):
        code, output = self._dry_run([])
        self.assertEqual(code, 0)
        self.assertIn("--- TEMPLATE ---", output)

    def _dry_run(self, argv):
        import io
        import prokrustes.cli as cli

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("a={v:/x/}")
        captured = io.StringIO()
        sys.stdout = captured
        try:
            code = cli.main(["-", "--dry-run"] + argv)
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        return code, captured.getvalue()


class TestThinkMode(unittest.TestCase):
    """--think constrains only the document after </think>."""

    def test_grammar_has_think_rule_first(self):
        import re as re_module

        compiled = compile_template("a={v:/x/}", use_prefill=False,
                                    think=True)
        self.assertTrue(compiled.think)
        self.assertIn("<think>", compiled.grammar)
        match = re_module.search(r"^root ::= (\S+)", compiled.grammar,
                                 re_module.M)
        self.assertIsNotNone(match)
        think_rule = match.group(1)
        self.assertIn('%s ::= "<think>"' % think_rule, compiled.grammar)

    def test_think_off_by_default(self):
        compiled = compile_template("a={v:/x/}", use_prefill=False)
        self.assertFalse(compiled.think)
        self.assertNotIn("<think>", compiled.grammar)

    def test_field_named_think_keeps_own_rule(self):
        import re as re_module

        compiled = compile_template("{think:/x/}", use_prefill=False,
                                    think=True)
        fields = [p for p in compiled.parts if isinstance(p, Field)]
        self.assertEqual([p.rule for p in fields], ["v-think"])
        match = re_module.search(r'(\S+) ::= "<think>"', compiled.grammar)
        self.assertIsNotNone(match)
        self.assertNotEqual(match.group(1), "v-think")
        self.assertTrue(compiled.grammar.splitlines()[-1].startswith(
            "root ::= %s " % match.group(1)))

    def test_think_grammar_round_trip(self):
        compiled = compile_template("a={v:/x/}", use_prefill=False,
                                    think=True)
        back = regex_for(compiled.grammar)
        self.assertTrue(back.fullmatch("<think>let me see</think>a=x"))
        self.assertTrue(back.fullmatch("<think></think>a=x"))
        self.assertTrue(back.fullmatch("<think>a<b and c</think>a=x"))
        self.assertFalse(back.fullmatch("a=x"))
        self.assertFalse(back.fullmatch("<think>unterminated a=x"))

    def test_split_thinking(self):
        from prokrustes.cli import split_thinking

        thinking, doc = split_thinking("<think>because reasons</think>a=x")
        self.assertEqual(thinking, "<think>because reasons</think>")
        self.assertEqual(doc, "a=x")

    def test_split_thinking_missing_close(self):
        from prokrustes.cli import split_thinking

        thinking, doc = split_thinking("<think>oops a=x")
        self.assertIsNone(thinking)
        self.assertEqual(doc, "<think>oops a=x")

    def test_split_thinking_stops_at_first_close(self):
        from prokrustes.cli import split_thinking

        thinking, doc = split_thinking("<think>a</think>b</think>c")
        self.assertEqual(thinking, "<think>a</think>")
        self.assertEqual(doc, "b</think>c")

    def test_think_prompt_asks_for_reasoning_first(self):
        compiled = compile_template("a={v:/x/}", use_prefill=False,
                                    think=True)
        prompt = build_prompt(compiled)
        self.assertIn("<think>", prompt)
        self.assertIn("</think>", prompt)
        self.assertNotIn("Output only the completed document.", prompt)

    def _run_main(self, template, completion_body, argv=()):
        import io
        import prokrustes.cli as cli

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        calls = []

        def fake_urlopen(req, timeout=None):
            if req.data is None:
                return FakeResponse({"data": [{"id": "m"}]})
            body = json.loads(req.data.decode("utf-8"))
            calls.append((req.full_url, body))
            if req.full_url.endswith("/apply-template"):
                return FakeResponse({"prompt": "<CHAT>\n"})
            return FakeResponse({"content": completion_body,
                                 "stop_type": "eos"})

        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO(template)
        out, err = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = out, err
        original = cli.urllib.request.urlopen
        cli.urllib.request.urlopen = fake_urlopen
        try:
            code = cli.main(["-", "--think", "--seed", "7",
                             "--temperature", "0", "--top-k", "1",
                             "--top-p", "1", "--n-predict", "64"]
                            + list(argv))
        finally:
            sys.stdin, sys.stdout, sys.stderr = \
                old_stdin, old_stdout, old_stderr
            cli.urllib.request.urlopen = original
        return code, out.getvalue(), err.getvalue(), calls

    def test_think_end_to_end(self):
        code, out, err, _ = self._run_main(
            "listen {port:/[0-9]+/};",
            "<think>port eighty</think>listen 80;")
        self.assertEqual(code, 0)
        self.assertEqual(out, "listen 80;")
        self.assertIn("<think>port eighty</think>", err)

    def test_think_keeps_leading_literal_in_grammar(self):
        _, _, _, calls = self._run_main(
            "listen {port:/[0-9]+/};",
            "<think>port eighty</think>listen 80;")
        completion = [p for u, p in calls if u.endswith("/completion")][0]
        # No prefill is used in think mode: the model emits the leading
        # literal itself, so the grammar must still contain it.
        self.assertIn('"listen "', completion["grammar"])

    def test_think_missing_close_falls_back_to_document(self):
        code, out, err, _ = self._run_main(
            "listen {port:/[0-9]+/};", "listen 80;")
        self.assertEqual(code, 0)
        self.assertEqual(out, "listen 80;")
        self.assertIn("no </think> found", err)


# --------------------------------------------------------------------------
# Broader properties
# --------------------------------------------------------------------------


class TestEquivalence(unittest.TestCase):
    """End to end: the grammar accepts exactly what the patterns accept."""

    TEMPLATE = "a={first:/%s/} b={second:/%s/}\n"

    def test_multi_field_equivalence(self):
        """With two fields, agreement must hold on both at once."""
        patterns = [r"[a-z]+", r"[0-9]{2,4}", r"x|y|z", r"[a-z]{2}(-[0-9]+)?"]
        pool = "axyz019- "
        rng = random.Random(7)
        for one, two in itertools.product(patterns, repeat=2):
            with self.subTest(one=one, two=two):
                compiled = compile_template(self.TEMPLATE % (one, two),
                                            use_prefill=False)
                regex = regex_for(compiled.grammar)
                for _ in range(200):
                    first = "".join(rng.choice(pool)
                                    for _ in range(rng.randint(0, 5)))
                    second = "".join(rng.choice(pool)
                                     for _ in range(rng.randint(0, 5)))
                    text = "a=%s b=%s\n" % (first, second)
                    expected = bool(re.fullmatch(one, first)) and bool(
                        re.fullmatch(two, second)
                    )
                    self.assertEqual(
                        bool(regex.match(text)),
                        expected,
                        "%r disagrees for %r / %r" % (text, one, two),
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
