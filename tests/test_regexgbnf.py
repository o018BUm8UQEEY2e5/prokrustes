"""Unit tests for the regex -> GBNF compiler.

The central property is equivalence: for every translatable pattern, the set of
strings the compiled grammar accepts must equal the set ``re.fullmatch``
accepts. Each test compiles the GBNF back into a Python regex (via
``gbnf2re``) and compares the two on concrete strings, so no model or server is
needed to check correctness.

Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import random
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from gbnf2re import regex_for
from prokrustes import regexgbnf
from prokrustes.regexgbnf import RegexSyntaxError, compile_pattern
from prokrustes.template import TemplateError, parse_field

ALPHABET = "ab01-_. /"


def compiled(pattern: str, flags: str = ""):
    return regex_for("root ::= " + compile_pattern(pattern, flags))


def candidates(pool: str, limit: int = 400):
    """Generate short strings over ``pool`` for differential testing."""
    out = [""]
    level = [""]
    for _ in range(3):
        nxt = []
        for base in level:
            for ch in pool:
                text = base + ch
                out.append(text)
                nxt.append(text)
        level = nxt
        if len(out) > limit:
            break
    return out[:limit]


class TestEquivalence(unittest.TestCase):
    """The grammar must accept exactly what ``re`` accepts."""

    PATTERNS = [
        r"[a-z]{2,8}",
        r"[a-z]+",
        r"\d{1,3}",
        r"\d+",
        r"\w{1,10}",
        r"\w+",
        r"\s",
        r"[abc]x",
        r"x[abc]",
        r"a[-b]c",
        r"[.]",
        r"[a-c-]",
        r"[-a-c]",
        r"[^\n]+",
        r"[^abc]+",
        r"foo|bar|baz",
        r"(?:ab)+c?",
        r"a?b*c+",
        r"(a|b)(c|d)",
        r"colou?r",
        r"^abc$",
        r"abc",
        r"[A-Z][a-z]*",
        r"[-_a-zA-Z0-9]{3,10}",
        r"a{3}",
        r"a{2,}",
        r"x[0-9a-f]{6}",
        r"\.",
        r"\\",
        r"[0-9]{1,3}\.[0-9]{1,3}",
        r"v\d+(\.\d+){2}",
        r"[a-z]{2}(-[a-z]{2})?",
        r"(GET|POST|PUT)",
        r"\s*\S+\s*",
        r"[A-F0-9]{8}",
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
        r"a\/b",
    ]

    def test_matches_re_exactly(self):
        pool = candidates(ALPHABET)
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                regex = compiled(pattern)
                reference = re.compile(pattern)
                for text in pool:
                    self.assertEqual(
                        bool(regex.match(text)),
                        bool(re.fullmatch(pattern, text)),
                        "%r vs %r disagree on %r"
                        % (regex.pattern, pattern, text),
                    )

    def test_escaped_slash_matches_python(self):
        regex = compiled(r"a\/b")
        self.assertIsNotNone(regex.match("a/b"))
        self.assertIsNone(regex.match("ab"))

    def test_flags_ignored_by_default(self):
        self.assertEqual(compile_pattern(r"abc"), compile_pattern(r"abc", ""))

    def test_ignorecase(self):
        regex = compiled("HELLO", "i")
        for text in ["hello", "Hello", "HELLO", "hEllO"]:
            self.assertIsNotNone(regex.match(text), text)
        self.assertIsNone(regex.match("help"))

    def test_ignorecase_class(self):
        regex = compiled("[a-c]+", "i")
        for text in ["abc", "ABC", "aBc"]:
            self.assertIsNotNone(regex.match(text), text)
        self.assertIsNone(regex.match("abd"))

    def test_ignorecase_with_literal_and_class(self):
        regex = compiled("v[0-9]+Alpha", "i")
        self.assertIsNotNone(regex.match("v12alpha"))
        self.assertIsNotNone(regex.match("V12ALPHA"))

    def test_dotall(self):
        self.assertIsNotNone(compiled("a.b", "s").match("a\nb"))
        self.assertIsNone(compiled("a.b").match("a\nb"))

    def test_dot_excludes_newline_by_default(self):
        strict = compiled("a.b")
        self.assertIsNone(strict.match("a\nb"))
        self.assertIsNotNone(strict.match("axb"))
        # A negated class differs: in Python [^x] does match "\n", and the
        # grammar preserves that.
        self.assertIsNotNone(compiled("[^x]+").match("a\nb"))


class TestRejections(unittest.TestCase):
    """Non-regular and unsupported constructs must raise, never mistranslate."""

    BAD = [
        (r"(a)\1", "backreference"),
        (r"(?=foo)", "lookahead"),
        (r"(?!foo)", "lookahead"),
        (r"(?<=foo)", "lookbehind"),
        (r"(?<!foo)", "lookbehind"),
        (r"(?>abc)", "atomic"),
        (r"\bword\b", "word boundary"),
        (r"\B", "non-boundary"),
        (r"\Aabc", "anchor"),
        (r"a\Q", "escape"),
        (r"[a-", "unterminated class"),
        (r"[a-\w]", "range to a shorthand"),
        (r"[[:alpha:]]", "POSIX"),
        (r"(a", "unclosed group"),
        (r"a{1,5000}", "too large"),
        (r"*abc", "nothing to repeat"),
        (r"a^", None),
    ]

    def test_rejected(self):
        for pattern, _hint in self.BAD:
            with self.subTest(pattern=pattern):
                with self.assertRaises((RegexSyntaxError, TemplateError)):
                    parse_field("v:/%s/" % pattern)

    def test_error_mentions_cause(self):
        with self.assertRaisesRegex(RegexSyntaxError, "backreference"):
            compile_pattern(r"(a)\1")
        with self.assertRaisesRegex(RegexSyntaxError, "lookahead"):
            compile_pattern(r"(?=x)")
        with self.assertRaisesRegex(RegexSyntaxError, "lookbehind"):
            compile_pattern(r"(?<=x)")

    def test_bad_flags(self):
        for flags in ["x", "m", "a", "u", "L", "U", "z"]:
            with self.subTest(flags=flags):
                with self.assertRaises(RegexSyntaxError):
                    compile_pattern("abc", flags)

    def test_invalid_re_is_reported(self):
        with self.assertRaises(RegexSyntaxError):
            compile_pattern("(unclosed")

    def test_negated_shorthand_in_class(self):
        with self.assertRaises(RegexSyntaxError):
            compile_pattern(r"[\D]")

    def test_shorthand_in_class(self):
        regex = compiled(r"[a-c\d]+")
        for text in ["abc", "123", "a1c"]:
            self.assertIsNotNone(regex.match(text), text)
        self.assertIsNone(regex.match("a-d"))

    def test_empty_match_class(self):
        with self.assertRaises(RegexSyntaxError):
            compile_pattern(r"[^\s\S]")


class TestFuzz(unittest.TestCase):
    """Differential test against ``re`` over randomly generated patterns.

    The hand-written cases check specific constructs; this searches for the
    precedence and grouping mistakes they might miss, by generating patterns
    and asserting the compiled grammar and ``re`` agree on random strings.
    """

    POOL = "ab01-_. x"

    def _pattern(self, rng, depth=0):
        roll = rng.random()
        if depth >= 2 or roll < 0.40:
            atom = rng.choice(self.POOL.replace(" ", ""))
            if atom in "-._":
                return re.escape(atom)
            if rng.random() < 0.20:
                lo, hi = sorted(rng.sample("abcxy013", 2))
                return "[%s-%s]" % (lo, hi)
            return re.escape(atom)
        if roll < 0.60:
            inner = "".join(self._pattern(rng, depth + 1)
                            for _ in range(rng.randint(1, 3)))
            return "(?:%s)" % inner
        if roll < 0.78:
            return "%s|%s" % (self._pattern(rng, depth + 1),
                              self._pattern(rng, depth + 1))
        quant = rng.choice(["*", "+", "?", "{1,3}", "{2}", "{0,2}"])
        return "%s%s" % (self._pattern(rng, depth + 1), quant)

    def test_agrees_with_re(self):
        rng = random.Random(20260902)
        pool = self.POOL.replace(" ", "")
        checked = 0
        for _ in range(250):
            pattern = self._pattern(rng)
            try:
                re.compile(pattern)
            except re.error:
                continue
            try:
                regex = compiled(pattern)
            except RegexSyntaxError:
                continue
            reference = re.compile(pattern)
            strings = [""] + [
                "".join(rng.choice(pool) for _ in range(rng.randint(1, 5)))
                for _ in range(60)
            ]
            for text in strings:
                checked += 1
                self.assertEqual(
                    bool(regex.match(text)),
                    bool(reference.fullmatch(text)),
                    "%r disagrees with %r on %r"
                    % (regex.pattern, pattern, text),
                )
        self.assertGreater(checked, 2000)


class TestSyntaxSafety(unittest.TestCase):
    def test_emitted_grammar_is_balanced(self):
        for pattern in TestEquivalence.PATTERNS:
            with self.subTest(pattern=pattern):
                regexgbnf.check_syntax("root ::= %s\n" % compile_pattern(pattern))

    def test_class_metacharacters_are_escaped(self):
        expr = compile_pattern(r"[\]\-\^\\]")
        regexgbnf.check_syntax("root ::= %s\n" % expr)
        regex = regex_for("root ::= " + expr)
        for ch in "]-^\\":
            self.assertIsNotNone(regex.match(ch), ch)


def accepts(pattern: str, text: str, flags: str = "") -> bool:
    """Python-side check used only by these tests."""
    unknown = sorted(set(flags) - set(regexgbnf.SUPPORTED_FLAGS))
    if unknown:
        raise RegexSyntaxError("unsupported flag %r" % unknown[0])
    flag_bits = 0
    if "i" in flags:
        flag_bits |= re.IGNORECASE
    if "s" in flags:
        flag_bits |= re.DOTALL
    return re.fullmatch(pattern, text, flag_bits) is not None


class TestAccepts(unittest.TestCase):
    def test_helper(self):
        self.assertTrue(accepts(r"\d+", "123"))
        self.assertFalse(accepts(r"\d+", "12a"))
        self.assertTrue(accepts("ABC", "abc", "i"))

    def test_bad_flag_raises(self):
        with self.assertRaises(RegexSyntaxError):
            accepts("abc", "ABC", "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
