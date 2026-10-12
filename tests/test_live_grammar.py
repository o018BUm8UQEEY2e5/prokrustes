"""Verify generated grammars against the real llama.cpp grammar parser.

These are integration tests: they need a running ``llama-server`` and are
skipped when one is not reachable, so ``python3 -m unittest discover -s tests``
still passes offline. No GPU and no generative model are needed -- llama-server
reports a grammar it cannot compile, and the grammar parser needs neither.

A grammar llama.cpp refuses surfaces at generation time only as "failed to parse
grammar", with no line number, so checking it here is the difference between a
release that works and one that breaks on the user's first run.

To run them, point the tests at a server::

    PROKRUSTES_LLAMA_URL=http://127.0.0.1:8080 python3 -m unittest discover -s tests

Any model will do -- grammar compilation does not depend on the weights, but
the server refuses to load weightless ``ggml-vocab-*.gguf`` files. Without the
environment variable the tests are skipped rather than failed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from prokrustes import regexgbnf
from prokrustes.cli import compile_template
from prokrustes.template import TemplateError

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)

# Patterns a template might plausibly use, grouped by the construct each probes.
PATTERNS = [
    # classes and ranges
    r"[a-z]{2,8}", r"[A-Z][a-z]*", r"[a-z0-9_-]+", r"[^\n]+", r"[abc]x",
    r"a[-b]c", r"[.]", r"[a-c-]", r"[-a-c]", r"[][\\^]",
    # shorthands
    r"\d+", r"\d{1,3}", r"\w+", r"\s", r"\S{2}", r"[a-c\d]+",
    # quantifiers
    r"a{3}", r"a{2,}", r"a{1,5}", r"a*", r"a+", r"a?",
    r"x{0,64}[0-9]{0,4}", r"[0-9]{1,3}(,[0-9]{1,3}){1,4}",
    # alternation and groups
    r"foo|bar|baz", r"(?:ab)+c?", r"a?b*c+", r"(a|b)(c|d)", r"colou?r",
    r"[a-z]{2}(-[a-z]{2})?", r"v\d+(\.\d+){2}", r"(GET|POST|PUT)",
    # realistic shapes
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    r"((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])",
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}",
    r"#[0-9a-fA-F]{6}", r"[a-z0-9]+(-[a-z0-9]+)*",
    r"[a-z][a-z0-9-]{1,20}\.[a-z]{2,6}",
    r"[A-Za-z][A-Za-z0-9 ,.]{4,40}", r"[A-Z][a-z]+ [A-Z][a-z]+",
    # escapes
    r"\.", r"\\", r"a\/b", r"\x41", r"[A-Z][a-z]?",
]

# Flags are exercised separately: they alter the emitted grammar.
FLAG_CASES = [(r"[a-z]+", "i"), (r"a.b", "s"), (r"a.b", "is")]

# Templates exercising brace escaping and neighbouring placeholders.
TEMPLATES = [
    "{v:/%s/}",
    "prefix {v:/%s/} suffix",
    "line1\n  {v:/%s/}\nline3\n",
    '{{"key": "{v:/%s/}", "n": {{1}}}}',
    "a={a:/x/} b={v:/%s/} c=end",
    "{v:/%s/}\n{v:/%s/}\n",
]

# Generators such as rgxg produce long alternations; they must survive the
# round trip through GBNF even though they never appear by hand.
GENERATED_CASES = [
    ("port 1..65535",
     r"(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}"
     r"|[1-5][0-9]{4}|[1-9][0-9]{0,3})"),
    ("cidr 192.168.0.0/24",
     r"192\.168\.0\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"),
    ("hex 0..255", r"[1-9A-Fa-f]?[0-9A-Fa-f]"),
]


def server_url() -> str | None:
    """The llama-server to test against, or None to skip."""
    url = os.environ.get("PROKRUSTES_LLAMA_URL", "").strip()
    if not url:
        return None
    return url.rstrip("/")


def server_available(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def post(url: str, payload: dict, timeout: float = 30.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body[:800]}


def grammar_rejected(result: dict) -> str | None:
    """Return a reason if the server signalled a grammar problem."""
    if "error" in result:
        return str(result["error"])[:400]
    content = result.get("content")
    if content is not None and not content:
        if result.get("timings", {}).get("predicted_n", 0) == 0:
            return "grammar compiled but nothing could be sampled (empty output)"
    return None


def _sources():
    """Every (label, template source) pair worth sending to the server."""
    for pattern in PATTERNS:
        for template in TEMPLATES:
            source = template.replace("%s", pattern)
            if source.count("%s") == 2:
                source = source.replace("%s", pattern, 1)
            yield "pattern %r" % pattern, source
    for pattern, flags in FLAG_CASES:
        yield "flags %r/%s" % (pattern, flags), "{v:/%s/%s}" % (pattern, flags)
    for label, pattern in GENERATED_CASES:
        yield "generated %s" % label, "{v:/%s/}" % pattern


def _bundled_examples():
    for name in sorted(os.listdir(EXAMPLES)):
        if not name.endswith(".tpl"):
            continue
        with open(os.path.join(EXAMPLES, name), encoding="utf-8") as handle:
            yield "example %s" % name, handle.read()


@unittest.skipUnless(
    server_url(), "set PROKRUSTES_LLAMA_URL to a running llama-server to run these"
)
class TestAgainstLlamaCpp(unittest.TestCase):
    """A generated grammar that llama.cpp refuses is a broken release."""

    @classmethod
    def setUpClass(cls) -> None:
        # Re-checked here rather than at import: a server that was starting when
        # the module loaded may be up by now, and probing must not happen during
        # collection.
        cls.base = server_url()
        if not server_available(cls.base):
            raise unittest.SkipTest("no llama-server responding at %s" % cls.base)
        cls.endpoint = cls.base + "/completion"

    def assert_accepted(self, grammar: str, label: str) -> None:
        _, result = post(
            self.endpoint,
            {
                "prompt": "x",
                "grammar": grammar,
                "n_predict": 1,
                "temperature": 0.0,
                "stream": False,
            },
        )
        reason = grammar_rejected(result)
        self.assertIsNone(reason, "%s rejected: %s" % (label, reason))

    def test_broken_grammar_is_rejected(self):
        """Control: if this passes silently the checks below prove little.

        llama.cpp rejects an unterminated literal, so a server that accepts it
        is not actually validating, which would make every other result here
        meaningless.
        """
        _, result = post(
            self.endpoint,
            {"prompt": "x", "grammar": 'root ::= "unterminated', "n_predict": 1},
        )
        self.assertIsNotNone(
            grammar_rejected(result),
            "server accepted a deliberately broken grammar, so it is not "
            "validating and these results prove nothing",
        )

    def test_patterns_and_shapes(self):
        checked = 0
        seen = set()
        for label, source in _sources():
            try:
                compiled = compile_template(source, use_prefill=False)
            except (TemplateError, regexgbnf.RegexSyntaxError) as exc:
                self.fail("%s did not compile: %r" % (label, exc))
            if compiled.grammar in seen:
                continue
            seen.add(compiled.grammar)
            with self.subTest(case=label):
                self.assert_accepted(compiled.grammar, label)
            checked += 1
        self.assertGreater(checked, 100, "pattern coverage collapsed")

    def test_bundled_examples(self):
        for label, source in _bundled_examples():
            with self.subTest(case=label):
                self.assert_accepted(compile_template(source).grammar, label)


if __name__ == "__main__":
    unittest.main()