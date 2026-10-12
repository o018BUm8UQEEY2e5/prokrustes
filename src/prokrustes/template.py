"""Template scanning.

A template is text with placeholders, and every placeholder carries a Python
regular expression:

    listen {port:/[1-9][0-9]{0,4}/};
    server_name {/\\.[a-z]{2,6}/};

The name before the colon is optional. It is the key a captured value is
reported under and the handle for pinning or for repeating one value in several
places, so it is only needed when you want to refer to a value:

    {port:/[1-9][0-9]{0,4}/}   named: can be pinned, and the same name
                               twice means one value
    {/[1-9][0-9]{0,4}/}         anonymous: positional internally, so a template
                               can hold two ports without renaming one port2

``{{`` and ``}}`` are literal braces. A regex may contain ``{`` and ``}`` of its
own, as in ``/a{2,3}/``, so the scanner cannot simply stop at the first ``}``;
it matches the placeholder's whole shape first and falls back to the first ``}``
only to report a better error.

Patterns are validated here, not at generation time, so a template with a bad
pattern fails immediately with a message naming the construct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# A whole placeholder, used to find its end without tripping over braces that
# belong to the pattern's own quantifiers. The pattern body may not contain an
# unescaped '/', which is what makes the delimiter unambiguous -- otherwise a
# greedy match would run past ``/{a:/x/}/{a:/y/}`` and merge two placeholders.
# Write ``\/`` for a literal slash; Python's re accepts that as a plain '/'.
# The name before the colon is optional, which is what lets a template hold two
# placeholders of the same kind without inventing names for them.
_PLACEHOLDER_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*:)?/(?:[^/\\]|\\.)*/[is]*\}"
)

SUPPORTED_FLAGS = "is"

_FLAG_HELP = {
    "i": "ignore case",
    "s": "dot matches newline",
}


class TemplateError(Exception):
    pass


@dataclass
class Field:
    """A placeholder: a pattern constraining the value, and a name.

    ``name`` is the name as written, or a positional ``fieldN`` when the
    placeholder was anonymous. ``anonymous`` distinguishes the two: an anonymous
    placeholder is not reported in the captured values and cannot be pinned, but
    it still needs an internal identity for rule naming and extraction.
    """

    name: str
    pattern: str
    flags: str = ""
    offset: int = 0
    source: str = ""
    rule: str = ""
    anonymous: bool = False

    @property
    def spec_text(self) -> str:
        """The placeholder body as written, for re-rendering a template."""
        if self.anonymous:
            return "/%s/%s" % (self.pattern, self.flags)
        return "%s:/%s/%s" % (self.name, self.pattern, self.flags)

    @property
    def pattern_text(self) -> str:
        """Just the pattern, for messages that already name the field."""
        return "/%s/%s" % (self.pattern, self.flags)

    def describe(self) -> str:
        return self.spec_text


def parse_field(body: str, offset: int = 0, index: int = 1) -> Field:
    """Parse the text between the braces of one placeholder.

    ``body`` is everything after ``{`` and before the closing ``}``. ``index`` is
    the placeholder's 1-based position, used to name anonymous ones.
    """
    if body.startswith("/"):
        pattern, flags = _split(body, offset, None)
        return _finish(name="field%d" % index, pattern=pattern, flags=flags,
                       offset=offset, source=body, anonymous=True)

    name, sep, spec = body.partition(":")
    name = name.strip()
    if not _NAME_RE.match(name):
        raise TemplateError(
            "invalid field name %r at offset %d; names look like Python "
            "identifiers" % (body, offset)
        )
    if not sep or not spec.strip():
        raise TemplateError(
            "field %r has no pattern; write it as {%s:/pattern/}, for example "
            "{%s:/[a-z][a-z0-9-]*/}, or drop the name: {/pattern/}"
            % (name, name, name)
        )
    pattern, flags = _split(spec.strip(), offset, name)
    return _finish(name=name, pattern=pattern, flags=flags, offset=offset,
                   source=body, anonymous=False)


def _split(spec: str, offset: int, name) -> tuple[str, str]:
    """Pull the pattern and flags out of ``/pattern/flags``."""
    label = name if name else "placeholder"
    if not spec.startswith("/"):
        raise TemplateError(
            "field %s must specify a regex, as {name:/pattern/} or "
            "{/pattern/}, not %r" % (label, spec)
        )
    close = spec.rfind("/")
    if close == 0:
        raise TemplateError(
            "field %s has no closing '/': %s" % (label, spec)
        )
    _unbalanced_slash(label, spec[1:close])
    pattern = spec[1:close]
    flags = spec[close + 1:].strip()
    if not pattern:
        raise TemplateError("field %s has an empty pattern" % label)

    unknown = sorted(set(flags) - set(SUPPORTED_FLAGS))
    if unknown:
        raise TemplateError(
            "field %s uses unsupported flag(s) %s; available flags are %s"
            % (
                label,
                ", ".join(repr(f) for f in unknown),
                ", ".join("%s (%s)" % (f, _FLAG_HELP[f]) for f in SUPPORTED_FLAGS),
            )
        )
    return pattern, flags


def _finish(name, pattern, flags, offset, source, anonymous) -> Field:
    _validate(name, pattern, flags)
    return Field(name=name, pattern=pattern, flags=flags, offset=offset,
                 source=source, anonymous=anonymous)


def _validate(name: str, pattern: str, flags: str) -> None:
    from .regexgbnf import RegexSyntaxError, compile_pattern

    try:
        compile_pattern(pattern, flags)
    except RegexSyntaxError as exc:
        raise TemplateError("field %r: %s" % (name, exc)) from None


def _placeholder_end(text: str, start: int) -> int:
    """Index of the ``}`` closing the placeholder beginning at ``start``.

    Falls back to the first ``}`` when the placeholder does not match the regex
    shape, so the caller reports the real problem instead of "unclosed {".
    """
    match = _PLACEHOLDER_RE.match(text, start + 1)
    if match:
        return match.end() - 1
    return text.find("}", start + 1)


def _unbalanced_slash(name: str, body: str) -> None:
    """Explain the likely cause when a pattern body contains a bare '/'.

    The delimiter is the last '/', and it is unambiguous only because a pattern
    may not contain an unescaped one. Reporting that directly is far clearer
    than the generic "no closing '/'" message.
    """
    index = 0
    while index < len(body):
        if body[index] == "\\":
            index += 2
            continue
        if body[index] == "/":
            raise TemplateError(
                "field %r contains an unescaped '/', which would be ambiguous "
                "with the delimiter; write \\\\/ for a literal slash"
                % name
            )
        index += 1


def scan(text: str) -> list[str | Field]:
    """Split template text into literal runs and placeholders."""
    parts: list[str | Field] = []
    buf: list[str] = []
    i = 0
    end = len(text)
    index = 0

    def flush() -> None:
        if buf:
            parts.append("".join(buf))
            buf.clear()

    while i < end:
        ch = text[i]
        if ch == "{":
            if text[i + 1:i + 2] == "{":
                buf.append("{")
                i += 2
                continue
            close = _placeholder_end(text, i)
            if close < 0:
                raise TemplateError("unclosed { at offset %d" % i)
            flush()
            index += 1
            parts.append(parse_field(text[i + 1:close], i, index))
            i = close + 1
            continue
        if ch == "}":
            if text[i + 1:i + 2] == "}":
                buf.append("}")
                i += 2
                continue
            raise TemplateError("unmatched } at offset %d (write }})" % i)
        buf.append(ch)
        i += 1

    flush()
    return parts


def render(parts: list[str | Field]) -> str:
    """Re-render scanned parts, for prompt bodies and error messages."""
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part.replace("{", "{{").replace("}", "}}"))
        else:
            out.append("{%s}" % part.spec_text)
    return "".join(out)
