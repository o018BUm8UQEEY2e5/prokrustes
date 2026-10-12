#!/usr/bin/env python3
"""Fill a text template, constraining a llama-server with a GBNF grammar.

Named for the Procrustes of Greek myth -- commonly spelled Prokrustes, as here
-- who made travellers fit his bed by cutting the tall and tying down the
short. "Procrustean" now means forcing something into a fixed shape regardless
of what it wanted to be. That is the mechanism here: the model supplies the
content, the grammar supplies the bed, and the output conforms to the shape
rather than merely looking like it does.

A template is text with placeholders, and each placeholder carries a Python
regular expression:

    listen {port:/[1-9][0-9]{0,4}/} ssl;
    server 10.0.{/[0-9]{1,3}/}.{/[0-9]{1,3}/}:{port2:/[1-9][0-9]{0,4}/};

The name is optional: it is the handle for pinning a value or for repeating
one value in several places, so leave it off when the value never needs
referring to. Literal text around the placeholders becomes fixed terminals in
the grammar and each placeholder becomes a rule compiled from its pattern.

The response is then re-parsed against the template and each value re-checked
with ``re``; a mismatch warns, and ``--strict`` turns the warning into a
non-zero exit. Only the filled document is ever printed.

Patterns are Python regular expressions. A hand-written pattern cannot express
a hard numeric bound -- ``/[1-9][0-9]{0,4}/`` also admits 99999 -- so for a
bound, paste in a generated alternation such as the output of
``rgxg range 1 65535``. See README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dc_field
from typing import Any

from . import unixsocket
from .gbnf import (
    Grammar,
    GrammarError,
    gbnf_literal,
    has_unbounded_repetition,
    rule_name,
)
from .regexgbnf import RegexSyntaxError, compile_pattern
from .template import Field, TemplateError, render, scan

DEFAULT_URL = "http://127.0.0.1:8080"
MAX_PREFILL = 200
MIN_PREFILL = 3


@dataclass
class Pinned:
    """A placeholder whose value the caller supplied, so it leaves the grammar."""

    name: str
    pattern: str
    flags: str
    value: str
    anonymous: bool = False

    @property
    def spec_text(self) -> str:
        return "/%s/%s" % (self.pattern, self.flags)


@dataclass
class Compiled:
    parts: list
    grammar: str
    prefill: str
    pinned: dict = dc_field(default_factory=dict)
    unbounded: list = dc_field(default_factory=list)
    think: bool = False


def _apply_pins(parts: list, pins: dict[str, str]) -> list:
    seen: set[str] = set()
    anonymous: dict[str, str] = {}
    out = []
    for part in parts:
        if isinstance(part, Field):
            if part.anonymous:
                # Anonymous placeholders are addressed by a generated id, so a
                # --field naming one is a mistake worth naming.
                anonymous[part.name] = part.spec_text
            else:
                seen.add(part.name)
                if part.name in pins:
                    out.append(
                        Pinned(part.name, part.pattern, part.flags,
                               pins[part.name])
                    )
                    continue
        out.append(part)
    unknown = sorted(set(pins) - seen)
    if unknown:
        lines = []
        for name in unknown:
            if name in anonymous:
                lines.append(
                    "%s is an anonymous placeholder (%s); give it a name to pin it"
                    % (name, anonymous[name])
                )
            else:
                lines.append("%s is not present in the template" % name)
        raise TemplateError("--field: " + "; ".join(lines))
    return out


def _check_ambiguity(parts: list) -> None:
    """Two adjacent placeholders could not be split apart after generation."""
    prev: Any = None
    for part in parts:
        is_value = isinstance(part, (Field, Pinned))
        if is_value and isinstance(prev, (Field, Pinned)):
            raise TemplateError(
                "two placeholders with no literal between them are ambiguous; "
                "the template cannot be parsed back"
            )
        prev = part


def _check_repeated_names(parts: list) -> None:
    """A name may repeat, but only with an identical pattern.

    Repeating a name is how a template says "the same value in both places",
    which a certificate and key basename need. Two different patterns under one
    name would be contradictory.
    """
    grouped: dict[str, list] = {}
    for part in parts:
        if isinstance(part, (Field, Pinned)):
            grouped.setdefault(part.name, []).append(part)
    for name, group in grouped.items():
        if len(group) < 2:
            continue
        specs = {(p.pattern, p.flags) for p in group}
        if len(specs) > 1:
            raise TemplateError(
                "field %r is used with two different patterns (%s); a repeated "
                "name must mean the same value"
                % (name, " and ".join(sorted("/%s/%s" % s for s in specs)))
            )


def _choose_prefill(parts: list, enabled: bool) -> str:
    if not enabled or not parts:
        return ""
    first = parts[0]
    if not isinstance(first, str) or not first:
        return ""
    lit = first
    if len(lit) > MAX_PREFILL:
        cut = lit.rfind("\n", 0, MAX_PREFILL)
        lit = lit[: cut + 1] if cut >= 0 else lit[:MAX_PREFILL]
    if len(lit) < MIN_PREFILL:
        return ""
    return lit


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# The thinking block is deliberately free-form: any characters except a nested
# closing tag. `<` alone is allowed (so reasoning can mention tags or code),
# but `</` is not, which makes the first `</think>` an unambiguous boundary
# for both the sampler and the extractor.
THINK_BODY = '([^<] | "<" [^/])*'
THINK_RULE = '"%s" %s "%s"' % (THINK_OPEN, THINK_BODY, THINK_CLOSE)


def compile_template(
    text: str, pins: dict[str, str] | None = None, use_prefill: bool = True,
    think: bool = False,
) -> Compiled:
    """Turn template text into a GBNF grammar plus an optional prefill prefix.

    With think=True the grammar is a thinking block followed by the document:
    the model's reasoning is unconstrained except for the delimiters, while
    the document after ``</think>`` is constrained exactly as usual.
    """
    parts = _apply_pins(scan(text), pins or {})
    _check_ambiguity(parts)
    _check_repeated_names(parts)
    if not parts:
        raise TemplateError("template is empty")

    for part in parts:
        if isinstance(part, Pinned):
            flags = re.compile(part.pattern, _re_flags(part.flags))
            if not flags.fullmatch(part.value):
                raise TemplateError(
                    "--field %s=%r does not match %s"
                    % (part.name, part.value, part.spec_text)
                )

    prefill = _choose_prefill(parts, use_prefill)
    g = Grammar()
    taken: set[str] = set()
    by_name: dict[str, str] = {}
    unbounded: list[str] = []
    seq: list[str] = []
    chunk: list[str] = []
    first = True

    def flush() -> None:
        if chunk:
            seq.append(gbnf_literal("".join(chunk)))
            chunk.clear()

    for part in parts:
        if isinstance(part, Field):
            flush()
            # A repeated name reuses the rule emitted for its first occurrence,
            # so the grammar offers the same shape in both places.
            name = by_name.get(part.name)
            if name is None:
                name = rule_name(part.name, taken)
                by_name[part.name] = name
                body = compile_pattern(part.pattern, part.flags)
                g.add(name, body)
                if has_unbounded_repetition(body):
                    unbounded.append(part.name)
            part.rule = name
            seq.append(name)
            first = False
            continue
        chunk_text = part.value if isinstance(part, Pinned) else part
        if first and prefill:
            chunk_text = chunk_text[len(prefill):]
            first = False
        if chunk_text:
            chunk.append(chunk_text)

    flush()
    if not seq:
        raise TemplateError("template has no text and no placeholders")
    if think:
        # The thinking rule is named last so a field called "think" keeps its
        # own rule and the block gets a suffixed one instead.
        think_rule = rule_name("think", taken)
        g.add(think_rule, THINK_RULE)
        seq.insert(0, think_rule)
    compiled = Compiled(parts=parts, grammar=g.render(" ".join(seq)), prefill=prefill)
    compiled.pinned = {p.name: p.value for p in parts if isinstance(p, Pinned)}
    compiled.unbounded = unbounded
    compiled.think = think
    return compiled


def _re_flags(flags: str) -> int:
    value = 0
    if "i" in flags:
        value |= re.IGNORECASE
    if "s" in flags:
        value |= re.DOTALL
    return value


def _hint(pattern: str, flags: str) -> str:
    """A short, concrete description of what a pattern asks for.

    The grammar is not injected into the prompt, so the model only learns the
    constraint from this text. Naming a familiar shape ("a hostname such as
    api.example.com") produces far better values than restating the regex, so
    well-known patterns get a description and the rest fall back to the pattern
    itself. Matching is on the pattern *source*: these are spellings a template
    is likely to use, not a general recogniser.
    """
    text = "".join(pattern.split())
    known = {
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}":
            "a UUID such as 7c88165f-1b6b-462a-87f5-10f9e4b3756c, with varied "
            "hex digits rather than all zeros or repeats",
        r"((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
        r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])":
            "an IPv4 address such as 10.0.1.1",
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}": "a plausible ISO date such as 2026-07-04",
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}": "a time of day such as 12:30:00",
        r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}": "a version number such as 1.4.2",
        r"#[0-9a-fA-F]{6}": "a #RRGGBB colour",
        r"[a-z][a-z0-9-]{1,30}\.[a-z]{2,6}": "a hostname such as api.example.com",
        r"[a-z0-9]+(-[a-z0-9]+)*":
            "a short lowercase name such as api-gateway, built from real words, "
            "never a run of digits",
    }
    if not flags:
        for source, description in known.items():
            if text == "".join(source.split()):
                return description
    suffix = {"i": ", ignoring case", "s": ", where '.' may cross lines"}
    extra = "".join(suffix[f] for f in flags if f in suffix)
    return "text matching this regex: %s%s" % (pattern, extra)


def build_prompt(compiled: Compiled, include_template: bool = True,
                 include_prompt: bool = True,
                 instructions: list[str] | None = None) -> str:
    """Instruction plus the template, with each named pattern described.

    The grammar is not injected into the prompt, so this text is the model's
    only view of the constraints. Named fields get a description, because
    naming them produced markedly better values in testing. Anonymous fields
    carry no name to describe them with, so the pattern is shown inline in the
    template instead and they are left out of the list -- unless the template
    itself is omitted, in which case there is no inline left and every field
    is listed.

    With include_prompt=False there are no instructions at all, only the
    prefill seed: the grammar alone must carry the shape. With instructions
    (lines read from --instructions PATH), the built-in preamble is replaced;
    the placeholder list and template block are still appended as usual.
    """
    if not include_prompt:
        return compiled.prefill
    if include_template:
        fields = [p for p in compiled.parts
                  if isinstance(p, Field) and not p.anonymous]
        default_preamble = [
            "You are completing a text template.",
            "Reproduce it exactly, replacing each {placeholder} with a single "
            "plausible value that matches the pattern given for it.",
            "Every value must look like the thing it stands for and fit its "
            "surroundings. Never pad: no runs of repeated digits, no repeated "
            "words, no zeros in an identifier, no placeholder-looking text.",
            "Do not add, remove, reorder or reformat any other character.",
            "Output only the completed document.",
        ]
        if compiled.think:
            default_preamble = [
                "You are completing a text template.",
                "First think step by step inside %s...%s about what value "
                "fits each placeholder."
                % (THINK_OPEN, THINK_CLOSE),
                "Then reproduce the template exactly, replacing each "
                "{placeholder} with a single plausible value that matches "
                "the pattern given for it.",
                "Every value must look like the thing it stands for and fit "
                "its surroundings. Never pad: no runs of repeated digits, no "
                "repeated words, no zeros in an identifier, no "
                "placeholder-looking text.",
                "Do not add, remove, reorder or reformat any other character.",
                "Output the completed document immediately after %s and "
                "nothing else." % THINK_CLOSE,
            ]
    else:
        fields = [p for p in compiled.parts if isinstance(p, Field)]
        default_preamble = [
            "You are completing a text template.",
            "For each placeholder below, invent a single plausible value that "
            "matches the pattern given for it. The fixed text around the "
            "placeholders is enforced separately, so concern yourself only "
            "with the values.",
            "Every value must look like the thing it stands for. Never pad: "
            "no runs of repeated digits, no repeated words, no zeros in an "
            "identifier, no placeholder-looking text.",
            "Output only the completed document.",
        ]
        if compiled.think:
            default_preamble = [
                "You are completing a text template.",
                "First think step by step inside %s...%s about what value "
                "fits each placeholder below. The fixed text around the "
                "placeholders is enforced separately, so concern yourself only "
                "with the values."
                % (THINK_OPEN, THINK_CLOSE),
                "Every value must look like the thing it stands for. Never "
                "pad: no runs of repeated digits, no repeated words, no zeros "
                "in an identifier, no placeholder-looking text.",
                "Output the completed document immediately after %s and "
                "nothing else." % THINK_CLOSE,
            ]
    lines = list(instructions) if instructions is not None else default_preamble
    if fields:
        lines.append("")
        lines.append("Placeholders:")
        listed: set[str] = set()
        for part in fields:
            if part.name in listed:
                continue
            listed.add(part.name)
            repeats = sum(1 for p in fields if p.name == part.name)
            entry = "  %s: %s" % (part.name, _hint(part.pattern, part.flags))
            if repeats > 1:
                entry += " (the same value is used at all %d places)" % repeats
            lines.append(entry)
    if include_template:
        lines += [
            "",
            "--- TEMPLATE ---",
            render(compiled.parts),
            "--- END TEMPLATE ---",
            "",
        ]
    return "\n".join(lines) + compiled.prefill


def _post_json(
    base_url: str, path: str, payload: dict, timeout: float, missing: str,
    unix_socket: str | None = None,
) -> dict:
    url = base_url.rstrip("/") + path
    if unix_socket is not None:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme != "http":
            raise SystemExit(
                "error: --unix-socket needs an http:// --url for the request "
                "path (got scheme %r)" % scheme
            )
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        if unix_socket is None:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        opener = unixsocket.build_opener(unix_socket)
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        # With --unix-socket the --url host is never dialed, so naming it
        # only confuses; show the endpoint path and the socket instead.
        where = ("POST %s via %s" % (path, unix_socket)
                 if unix_socket is not None else url)
        if exc.code == 404:
            raise SystemExit(
                "error: %s returned 404. %s" % (where, missing)
            ) from None
        raise SystemExit("error: %s returned %d\n%s"
                         % (where, exc.code, detail)) from None
    except urllib.error.URLError as exc:
        if unix_socket is None:
            target = url
            hint = ("Start a server first, e.g.\n"
                    "  llama-server -m /path/to/model.gguf --host 127.0.0.1 "
                    "--port 8080 -c 4096")
        else:
            target = unix_socket
            hint = ("Start a server first, e.g.\n"
                    "  llama-server -m /path/to/model.gguf "
                    "--host /tmp/llama-server.sock -c 4096")
        raise SystemExit(
            "error: cannot reach %s (%s)\n%s" % (target, exc.reason, hint)
        ) from None


def _get_json(
    base_url: str, path: str, timeout: float,
    unix_socket: str | None = None,
) -> dict:
    """GET a JSON document, over TCP or a Unix socket.

    Unlike _post_json this never aborts the program: callers use it for
    best-effort discovery (e.g. the server's model list) and fall back when
    the endpoint is missing. Returns {} on any failure.
    """
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET",
    )
    try:
        if unix_socket is None:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        opener = unixsocket.build_opener(unix_socket)
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def server_model(
    base_url: str, timeout: float, unix_socket: str | None = None,
) -> str | None:
    """The first model id the server reports via /v1/models, if any.

    Current llama-server builds require a "model" field on /apply-template
    (400 "model name is missing from the request" otherwise), but older ones
    have no /v1/models endpoint at all -- hence the silent fallback to None,
    which means "send no model field", i.e. the old behaviour.
    """
    result = _get_json(base_url, "/v1/models", timeout, unix_socket)
    entries = result.get("data")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            return entry["id"]
    return None


def post_completion(base_url: str, payload: dict, timeout: float,
                    unix_socket: str | None = None) -> dict:
    return _post_json(
        base_url,
        "/completion",
        payload,
        timeout,
        "This build of llama-server may not expose /completion; check the "
        "endpoint or start a server that does.",
        unix_socket,
    )


def build_chat_messages(compiled: Compiled,
                        include_template: bool = True,
                        include_prompt: bool = True,
                        instructions: list[str] | None = None
                        ) -> list[dict[str, str]]:
    """Put our instruction/template, unchanged, into a chat user message.

    The server's chat template supplies the model-specific system/user/assistant
    markers. Our document grammar is sent separately and still constrains only
    the generated text. With include_prompt=False the message is empty: the
    wrapper and prefill seed are the whole prompt.
    """
    content = (build_prompt(compiled, include_template, include_prompt,
                            instructions) if include_prompt
               else "")
    return [{"role": "user", "content": content}]


def apply_chat_template(
    base_url: str, messages: list[dict[str, str]], timeout: float,
    unix_socket: str | None = None, model: str | None = None,
) -> str:
    """Render chat messages through the server's chat template.

    Delegating the Jinja rendering to llama-server avoids reimplementing every
    model-specific template client-side. The returned string is prompt text, so
    it is combined with the same document grammar used in raw mode.

    Current server builds reject /apply-template without a "model" field
    (400 "model name is missing from the request"); older ones ignore the
    extra field, so it is always sent when known.
    """
    payload: dict = {"messages": messages}
    if model is not None:
        payload["model"] = model
    result = _post_json(
        base_url,
        "/apply-template",
        payload,
        timeout,
        "This build of llama-server may not expose /apply-template, which "
        "--chat needs; update the server or omit --chat.",
        unix_socket,
    )
    prompt = result.get("prompt")
    if not isinstance(prompt, str):
        raise SystemExit(
            "error: /apply-template response has no 'prompt'; got keys: %s"
            % ", ".join(sorted(result))
        )
    return prompt


def fill(url: str, compiled: Compiled, args: argparse.Namespace,
         instructions: list[str] | None = None) -> str:
    include_template = getattr(args, "template_in_prompt", True)
    include_prompt = not getattr(args, "no_prompt", False)
    unix_socket = getattr(args, "unix_socket", None)
    # Current server builds want a "model" on requests that name one;
    # /apply-template rejects the call without it. Prefer an explicit --model,
    # otherwise ask the server what it serves; unknown on old servers, in which
    # case nothing is sent and behaviour is unchanged.
    model = getattr(args, "model", None)
    if model is None:
        model = server_model(url, args.timeout, unix_socket)
    if getattr(args, "chat", True):
        # The wrapper lives in the prompt, not the grammar: ask the server to
        # place our user message inside its chat template, then constrain the
        # generated continuation with the unchanged document grammar. A leading
        # prefill literal is still supplied as the generation prefix, exactly as
        # in raw mode, so extraction sees the same document shape.
        prompt = (
            apply_chat_template(
                url,
                build_chat_messages(compiled, include_template,
                                    include_prompt, instructions),
                args.timeout,
                unix_socket,
                model,
            )
            + compiled.prefill
        )
    else:
        prompt = build_prompt(compiled, include_template, include_prompt,
                              instructions)
    payload = {
        "prompt": prompt,
        "grammar": compiled.grammar,
        **({"model": model} if model is not None else {}),
        "n_predict": args.n_predict,
        "temperature": args.temperature,
        "top_k": args.top_k,
        "top_p": args.top_p,
        "seed": args.seed,
        "dynatemp_range": args.dynatemp,
        "cache_prompt": False,
        "stream": False,
    }
    if args.stop:
        payload["stop"] = args.stop
    result = post_completion(url, payload, args.timeout, unix_socket)
    content = result.get("content")
    if content is None:
        content = result.get("text")
    if content is None:
        raise SystemExit(
            "error: no 'content' in server response; got keys: %s"
            % ", ".join(sorted(result))
        )
    if result.get("stop_type") == "limit" or result.get("truncated"):
        print(
            "warning: generation hit n_predict=%d, output may be truncated"
            % args.n_predict,
            file=sys.stderr,
        )
    return content


def extract(
    parts: list, document: str
) -> tuple[dict[str, str], str | None, dict[str, list]]:
    """Recover each placeholder's raw text by walking the known literals.

    Returns the values, an error message, and the span each occurrence occupies
    so a repeated name can be reconciled.
    """
    values: dict[str, str] = {}
    positions: dict[str, list] = {}
    pos = 0
    for idx, part in enumerate(parts):
        if isinstance(part, str):
            if not document.startswith(part, pos):
                return {}, "output diverged from the template at offset %d" % pos, {}
            pos += len(part)
            continue
        if isinstance(part, Pinned):
            if not document.startswith(part.value, pos):
                return {}, "pinned value %r missing at offset %d" % (part.value, pos), {}
            pos += len(part.value)
            continue
        anchor = ""
        for later in parts[idx + 1:]:
            if isinstance(later, str) and later:
                anchor = later
                break
            if isinstance(later, Pinned):
                anchor = later.value
                break
        start = pos
        if anchor:
            end = document.find(anchor, pos)
            if end < 0:
                return {}, "value for %r is unterminated (output truncated?)" % part.name, {}
            pos = end
        else:
            pos = len(document)
        values.setdefault(part.name, document[start:pos])
        positions.setdefault(part.name, []).append((start, pos))
    return values, None, positions


def validate(parts: list, raws: dict[str, str]) -> tuple[dict, list]:
    """Re-check every value against its pattern.

    The grammar should already guarantee this, so a failure means the output was
    truncated, the server ignored the grammar, or a repeated field was rewritten.
    Every placeholder is checked, including anonymous ones, but only named
    fields are returned: an anonymous placeholder exists for its shape, and the
    caller never asked to be handed its value. Values stay strings; patterns
    describe text, not JSON types.
    """
    values: dict[str, str] = {}
    problems: list[str] = []
    for part in parts:
        if not isinstance(part, Field) or part.name in values:
            continue
        raw = raws.get(part.name, "")
        try:
            if not re.fullmatch(part.pattern, raw, _re_flags(part.flags)):
                raise ValueError("does not match %s" % part.pattern_text)
        except re.error as exc:
            problems.append("%s: pattern failed to compile (%s)" % (part.name, exc))
        except ValueError as exc:
            problems.append("%s: %r %s" % (part.name, raw, exc))
        if not part.anonymous:
            values[part.name] = raw
    return values, problems


def rebuild(parts: list, values: dict[str, str]) -> str:
    """Re-emit the document from the template and the resolved values.

    A repeated field name means "the same value here and there", but a grammar
    cannot tie two separate positions together, so the model will sometimes emit
    two different values for one name. Rebuilding makes the first occurrence
    authoritative and copies it everywhere, which is the only place the
    equivalence can be enforced.
    """
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, Pinned):
            out.append(part.value)
        else:
            out.append(values.get(part.name, ""))
    return "".join(out)


def split_thinking(document: str) -> tuple[str | None, str]:
    """Split generated output into its thinking block and the document.

    Returns (thinking, document). The thinking block runs from ``<think>`` to
    the first ``</think>``; everything after is the document, parsed exactly
    as in non-think mode. If there is no closing tag the output cannot be
    trusted to contain a document at all, so thinking is None and the whole
    output is returned for the caller to handle as a divergence.
    """
    open_at = document.find(THINK_OPEN)
    start = open_at if open_at >= 0 else 0
    close_at = document.find(THINK_CLOSE, start)
    if close_at < 0:
        return None, document
    end = close_at + len(THINK_CLOSE)
    return document[start:end], document[end:]


def _pin_map(items: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit("error: --field expects name=value, got %r" % item)
        key, _, value = item.partition("=")
        pins[key.strip()] = value
    return pins


def _read_instruction_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise SystemExit("error: cannot read %s: %s" % (path, exc)) from None
    return text.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prokrustes",
        description=(
            "Fill a template with values matching per-placeholder regexes, using "
            "a llama-server grammar."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "template syntax:\n"
            "  {name:/pattern/}       placeholder, filled with text matching\n"
            "                        the Python regex 'pattern'; the name is\n"
            "                        the handle for pinning, and repeating a name\n"
            "                        means the same value in both places\n"
            "  {/pattern/}            the same, unnamed; use when you do not\n"
            "                        need to refer to the value, so two of a\n"
            "                        kind need no invented names\n"
            "  flags: i ignore case, s dot also matches newline\n"
            "\n"
            "  {{ and }} are literal braces, exactly as in an f-string.\n"
            "  A pattern may contain / and braces; the last / ends it, so an\n"
            "  escaped \\/ is needed for a literal slash.\n"
            "\n"
            "  examples:\n"
            "    {port:/[1-9][0-9]{0,4}/}          digits, 1-5 long\n"
            "    {/[1-9][0-9]{0,4}/}              the same, unnamed\n"
            "    {host:/[a-z][a-z0-9-]*\\.[a-z]{2,6}/}\n"
            "    {env:/prod|staging|dev/}         one of three\n"
            "\n"
            "  A hand-written pattern cannot express a hard numeric bound:\n"
            "  /[1-9][0-9]{0,4}/ also admits 99999. For a bound, paste in a\n"
            "  generated alternation such as the output of `rgxg range 1 65535`.\n"
            "  Patterns cannot express calendar validity, so 2026-02-31 passes\n"
            "  /\\d{4}-\\d{2}-\\d{2}/.\n"
            "  Unsupported constructs are rejected: backreferences, lookahead,\n"
            "  lookbehind, \\b, inline flags, atomic groups, POSIX classes.\n"
        ),
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="llama-server base URL")
    parser.add_argument("--model", default=None, metavar="NAME",
                        help="model name as listed by the server's /v1/models; "
                             "sent on /apply-template (which current servers "
                             "require) and /completion. Defaults to the first "
                             "model the server reports.")
    parser.add_argument("--unix-socket", metavar="PATH", default=None,
                        help="connect over a Unix domain socket instead of TCP, "
                             "curl --unix-socket style; the --url host is still "
                             "sent as the HTTP Host header")
    parser.add_argument("template", metavar="PATH",
                        help="template file, or - for stdin")
    parser.add_argument("--field", action="append", default=[], metavar="NAME=VALUE",
                        help="pin a named placeholder; validated and removed "
                             "from the grammar")
    parser.add_argument("--seed", type=int, default=-1, help="-1 for a random seed")
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--dynatemp", type=float, default=0.0, help="dynamic temp range")
    parser.add_argument("--n-predict", type=int, default=-1,
                        help="-1 for no limit (the default); the grammar still "
                             "ends generation once the document is complete")
    parser.add_argument("--stop", action="append", default=[], help="extra stop string")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--no-prefill", action="store_true",
                        help="do not seed the prompt with the leading literal")
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--template-in-prompt",
                              action=argparse.BooleanOptionalAction, default=True,
                              help="include the template text in the prompt (default: "
                                   "enabled; --no-template-in-prompt sends only the "
                                   "instructions and placeholder patterns)")
    prompt_group.add_argument("--no-prompt", action="store_true",
                              help="send no instructional prompt at all: an empty "
                                   "user message in chat mode, or just the prefill "
                                   "seed in raw mode. The grammar still constrains "
                                   "the output. Conflicts with --template-in-prompt, "
                                   "which is meaningless without a prompt.")
    parser.add_argument("--instructions", metavar="PATH",
                        help="read custom instruction lines from PATH, replacing "
                             "the built-in preamble; the placeholder list and "
                             "template block are still appended")
    parser.add_argument("--think", action="store_true",
                        help="let the model reason inside <think>...</think> "
                             "before the document; the thinking is printed to "
                             "stderr and stdout stays the filled document. The "
                             "thinking shares the --n-predict budget and may "
                             "not contain '</think>'.")
    parser.add_argument("--chat", action=argparse.BooleanOptionalAction, default=True,
                        help="send the prompt through the server's chat template "
                             "(/apply-template) before /completion; the grammar "
                             "still constrains only the filled template "
                             "(default: enabled; --no-chat uses raw /completion)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and grammar, then exit")
    parser.add_argument("--explain", action="store_true",
                        help="print each placeholder's pattern and grammar rule")
    parser.add_argument("--grammar-out", metavar="PATH", help="also write the GBNF")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any value fails validation")
    args = parser.parse_args(argv)

    if args.no_prompt and args.instructions is not None:
        parser.error("--instructions cannot be used with --no-prompt")

    instruction_lines = (_read_instruction_lines(args.instructions)
                         if args.instructions is not None else None)

    if args.template == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(args.template, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise SystemExit("error: cannot read %s: %s" % (args.template, exc)) from None

    # In think mode the model starts by reasoning, not by emitting the
    # document's leading literal, so there is nothing to prefill with. The
    # grammar still carries the full leading literal, which the model emits
    # itself after </think>.
    use_prefill = not args.no_prefill and not args.think
    try:
        compiled = compile_template(text, _pin_map(args.field), use_prefill,
                                    think=args.think)
    except (TemplateError, GrammarError) as exc:
        raise SystemExit("error: %s" % exc) from None

    if args.grammar_out:
        with open(args.grammar_out, "w", encoding="utf-8") as handle:
            handle.write(compiled.grammar)

    for part in compiled.parts:
        if isinstance(part, Field) and part.name in compiled.unbounded:
            where = ("field %r" % part.name if not part.anonymous
                     else "the anonymous placeholder %r" % part.name)
            print(
                "warning: %s has an unbounded repetition (* or +); a small "
                "model may never stop, which truncates the document. Bound it "
                "with {m,n}, e.g. change + to {1,32}." % where,
                file=sys.stderr,
            )

    if args.explain:
        for part in compiled.parts:
            if isinstance(part, Field):
                label = part.name + " (anonymous)" if part.anonymous else part.name
                print("%-22s %-40s %s" % (label, part.spec_text, part.rule))
        for part in compiled.parts:
            if isinstance(part, Pinned):
                print("%-22s %-40s (pinned %r)"
                      % (part.name, part.spec_text, part.value))

    if args.dry_run:
        include_prompt = not args.no_prompt
        if args.chat:
            print(json.dumps(build_chat_messages(compiled,
                                                 args.template_in_prompt,
                                                 include_prompt,
                                                 instruction_lines),
                             indent=2, ensure_ascii=False))
            print("--- CHAT TEMPLATE ---")
            print("(the server applies its chat template to the message above; "
                  "the grammar below is unchanged)")
        else:
            print(build_prompt(compiled, args.template_in_prompt,
                               include_prompt, instruction_lines))
        print("--- GRAMMAR ---")
        print(compiled.grammar, end="")
        return 0

    content = fill(args.url, compiled, args, instruction_lines)
    document = compiled.prefill + content
    if compiled.think:
        thinking, document = split_thinking(document)
        if thinking is None:
            print("warning: no %s found; treating all output as the document"
                  % THINK_CLOSE, file=sys.stderr)
        else:
            print(thinking, file=sys.stderr)
    raws, problem, positions = extract(compiled.parts, document)
    if problem:
        print("warning: %s" % problem, file=sys.stderr)
        sys.stdout.write(document)
        return 0 if not args.strict else 1

    # A repeated name is one value, but the grammar cannot require two separate
    # positions to agree, so the model sometimes emits two. Rebuild from the
    # template and the first occurrence, then re-parse to confirm.
    if any(
        document[start:end] != raws[name]
        for name, spans in positions.items()
        for start, end in spans[1:]
    ):
        for name, spans in positions.items():
            if any(document[s:e] != raws[name] for s, e in spans[1:]):
                print(
                    "note: field %r is used more than once; the model gave "
                    "different values, so all uses were set to %r"
                    % (name, raws[name]),
                    file=sys.stderr,
                )
                break
        document = rebuild(compiled.parts, raws)
        raws, problem, positions = extract(compiled.parts, document)
        if problem:
            print("warning: %s" % problem, file=sys.stderr)
            sys.stdout.write(document)
            return 0 if not args.strict else 1

    _, problems = validate(compiled.parts, raws)
    for line in problems:
        print("warning: %s" % line, file=sys.stderr)

    sys.stdout.write(document)
    return 1 if (problems and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
