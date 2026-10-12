# prokrustes

Fill a text template with values constrained by regular expressions, using a GBNF
grammar to constrain a llama.cpp server so it can only emit a correctly shaped
document.

Every placeholder carries a Python regular expression. Literal text around the
placeholders becomes fixed terminals in the grammar and each placeholder becomes
a rule compiled from its pattern:

```
listen {port:/[1-9][0-9]{0,4}/} ssl;
server_name {host:/[a-z][a-z0-9-]{1,30}\.[a-z]{2,6}/};
```

The response is then re-parsed against the template and each value re-checked
with `re`; a mismatch warns, and `--strict` turns the warning into a non-zero
exit. Only the filled document is ever printed.

## Requirements

- Python 3.9+ (standard library only)
- A running `llama-server`

```bash
llama-server -m /path/to/model.gguf --host 127.0.0.1 --port 8080 -c 4096
```

## Usage

```bash
pip install prokrustes

prokrustes examples/nginx.tpl
prokrustes - < mytemplate.tpl   # stdin
prokrustes examples/nginx.tpl --dry-run # show prompt+grammar
prokrustes examples/nginx.tpl --seed 7 --field port=8443
prokrustes examples/nginx.tpl --instructions preamble.txt
```

From a checkout, without installing, use `python3 -m prokrustes` with
`PYTHONPATH=src`.

`--dry-run` needs no server and is the fastest way to check a template.
`--field name=value` pins a value: it is checked against the pattern,
substituted literally, and removed from the grammar.
Chat mode is on by default: the instruction/template is sent to
`/apply-template`, then the rendered chat prompt is posted to `/completion`
with the same document grammar. The chat wrapper stays in the prompt; it is not
added to the grammar, and the returned text is still only the filled template.
Use `--no-chat` for raw `/completion` without the server's chat template.
Current server builds require a `model` name on `/apply-template` (they answer
400 otherwise); prokrustes sends the `--model` you give it, or otherwise the
first model the server lists on `/v1/models`. The same name is also sent on
`/completion`, which older servers simply ignore.
The template text itself is included in the prompt by default; pass
`--no-template-in-prompt` to send only the instructions and placeholder
patterns instead, which saves input tokens on large templates at some cost to
value quality. `--no-prompt` goes further and sends no instructional prompt at
all — an empty user message in chat mode, or just the prefill seed in raw
mode — leaving the grammar alone to carry the shape. It conflicts with
`--template-in-prompt`, which is meaningless without a prompt.
`--instructions PATH` replaces the built-in preamble with your own lines; the
placeholder list and template block are still appended. It conflicts with
`--no-prompt`, which would leave the file unread. The grammar is
unchanged in all cases.
`--think` lets the model reason first: generation starts with a free-form
`<think>...</think>` block, and only the document after `</think>` is
constrained by the grammar, parsed, and printed. The thinking goes to stderr,
so stdout stays the filled document. Thinking shares the `--n-predict` budget
with the document, and may not contain `</think>`.

To talk to a server listening on a Unix domain socket instead of TCP, pass
`--unix-socket`, curl style. The `--url` host is still sent as the HTTP `Host`
header; only `http://` URLs are supported:

```bash
llama-server -m /path/to/model.gguf --host /tmp/llama-server.sock -c 4096
prokrustes examples/nginx.tpl --unix-socket /tmp/llama-server.sock
```

Bundled templates: `nginx.tpl`, `config.json.tpl` (brace escaping),
`service.tpl`, `k8s.tpl`.

## Syntax

Every placeholder is a Python regular expression:

```
{name:/pattern/flags}    named: pinnable, and repeating the name means
                         the same value in both places
{/pattern/flags}         unnamed: use when you never refer to the value
```

`{{` and `}}` are literal braces, exactly as in an f-string, so brace-heavy
templates need no other escaping. A pattern may contain `{`, `}` and `|`
freely.

Flags mirror `re`: `i` ignore case, `s` dot also matches newline. `m`, `x`, `a`,
`u` and `L` are rejected — see the limitations below.

```bash
{port:/[1-9][0-9]{0,4}/}                       digits, 1-5 long
{/[1-9][0-9]{0,4}/}                           the same, unnamed
{env:/production|staging|dev/}                 one of three
{date:/[0-9]{4}-[0-9]{2}-[0-9]{2}/}            ISO-shaped date
{flags:/[a-z]{3}(,[a-z]{3}){1,5}/}             comma separated list
{url:/https?:\/\/[a-z0-9.-]+/}                 a URL (see note on '/')
```

### Why the name is optional

The name is the handle for referring to the value, so it is needed only
when you want to refer to the value. Leaving it off keeps the template
readable when a field has no life outside the document, and — because names must
otherwise be unique — it avoids inventing names like `port2`:

```
server 10.0.{/[0-9]{1,3}/}.{/[0-9]{1,3}/}:{port:/[1-9][0-9]{0,4}/};
```

An unnamed placeholder gets a positional id internally (`field1`, `field2`,
visible with `--explain`) so the grammar and extraction still work. It is
checked against its pattern like any other, but it is **not reported** and
**cannot be pinned** — naming it is what makes it addressable. `--field
field1=x` therefore fails with a message saying so.

Named fields get one further advantage: the prompt describes them in words, and
that measurably improved the values in testing. An unnamed placeholder has no
name to describe it with, so its pattern is shown inline in the template instead
and it is omitted from the prompt's placeholder list.

**A `/` inside a pattern must be escaped as `\/`.** The slash is the delimiter,
so an unescaped one would be ambiguous — with two placeholders side by side a
greedy match could swallow `/{a:/x/}/{b:/y/}` as one. Python's `re` treats `\/`
as a plain `/`, so nothing is lost.

## What regex cannot express

Patterns are the only placeholder language here, which buys one grammar engine
and one validator instead of two, but it costs some expressiveness. These were
available with named types:

| Wanted | Now | Exact? |
|---|---|---|
| port `1..65535` | `rgxg range 1 65535` | yes |
| IP in a CIDR block | `rgxg cidr 192.168.0.0/24` | yes |
| hex or other base | `rgxg range -b 16 0 255` | yes |
| at most N digits | `/[0-9]{1,N}/` | yes |
| calendar date | `/[0-9]{4}-[0-9]{2}-[0-9]{2}/` | no, admits `2026-02-31` |
| UUID, email, semver | a literal pattern for each | yes, but verbose |

Numeric ranges and CIDR blocks are only *awkward*, not impossible: the matching
string is long but mechanical, so generate it instead of hand-writing it.

**Numeric ranges used to be the real gap.** No regular expression can say
"between 1 and 65535" as a *range* — that is not a regular property. But a
regular expression *can* match exactly that set of strings, and it is a short
mechanical string to generate, so use [rgxg](https://rgxg.github.io/) rather
than hand-writing it:

```bash
$ rgxg range 1 65535
(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3})
```

Paste that straight into a placeholder and the bound is genuinely enforced by
the grammar, not just re-checked afterwards:

```
listen {port:/(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3})/} ssl;
```

`65536` and `99999` become ungrammatical, so the model cannot emit them even if it
wants to. `rgxg` also handles CIDR blocks (`rgxg cidr 192.168.0.0/24`) and other
bases (`rgxg range -b 16 0 31`), which covers the IP row in the table above.

Without `rgxg`, the fallback is a hand-written alternation, and note the
consequence: the grammar can still not *know* the bound. If the pattern admits
`99999`, `re` accepts it after generation too, so `--strict` will not catch it.

The same applies to dates: a pattern enforces the *shape* `2026-02-31` has, not
its validity. If you care, validate downstream.

## Rejected constructs

A regex is compiled to GBNF, which describes *regular* languages. Python's `re`
is a superset of that, so the following are refused at parse time with a message
naming the cause:

| Pattern | Reason |
|---|---|
| `(a)\1` | backreferences are not regular |
| `(?=x)` `(?!x)` | lookahead is not regular |
| `(?<=x)` `(?<!x)` | lookbehind is not regular |
| `\b` `\B` | word boundaries need surrounding context |
| `(?i)` | inline flags; use the `/…/i` suffix |
| `(?>x)` | atomic groups |
| `[[:alpha:]]` | POSIX classes |
| `\D` inside a class | negated shorthand in a character class |
| `a{1,5000}` | too many repetitions for GBNF |

These are refusals rather than approximations on purpose: a compiler that
silently accepted less than it appeared to would let through a template whose
grammar rejects values the pattern allows. Every translated grammar is a
*subset* of what `re` accepts, never a superset.

Two asymmetries worth knowing:

- `.` excludes newline unless you pass `s`, matching Python. A negated class
  differs: `[^x]` *does* match `\n` in Python, and the grammar preserves that.
- `i` folds ASCII only. Python's `re` also folds cases like `K` (U+212A) to
  `k` under Unicode rules, which no character class can express, so the grammar
  is the narrower of the two.

Repetition bounds are capped at 1000 and compiled expressions at 3000
characters, because GBNF expands `{m,n}` into `n` rules and llama.cpp refuses a
grammar past its own threshold — reported only as a parse failure.

## Values

Every named value is re-checked against its pattern with `re` after generation,
and a mismatch warns; `--strict` turns that warning into a non-zero exit. Values
stay strings: patterns describe text, not types.

## Behaviour worth knowing

- The grammar is **not** injected into the prompt; llama.cpp cannot do that. The
  prompt therefore restates each pattern in words — a well-known pattern like a
  UUID gets "a UUID such as 7c88165f-…", which produces far better values than
  repeating the regex — and the leading literal of the template is used as a
  prefill so the model starts inside the document. `--no-prefill` disables it.
- A field name may be **repeated** to mean "the same value in both places", which
  a certificate and key basename need. Both occurrences share one grammar rule,
  but a grammar cannot force two positions to agree, so the first occurrence wins
  and the rest are rewritten; a note on stderr says when that happened. Two
  occurrences of one name must use the same pattern. Unnamed placeholders cannot
  express this, since they have no identity to repeat.
- Bound your own repetitions. `/[a-z]+/` is legal, but a small model may never
  stop and the document gets truncated mid-value, so `prokrustes` warns about any
  field whose pattern has an unbounded `*` or `+`. Prefer `/[a-z]{1,20}/`.
- A repeated quantifier bound is a *maximum*, and models treat it as a target:
  given `[a-z]{2,8}` you will see `abcdefgh` rather than `api`. Set bounds close
  to a realistic value, and expect to tighten them after looking at output.
- Two adjacent placeholders with no literal between them are rejected, because
  the output could not be split back apart unambiguously.
- Quality depends on the model. A 1.5B instruct model fills these well; a 135M
  base model produces valid but meaningless values. Grammar guarantees *shape*,
  not sense.

## Files

| File | Role |
|---|---|
| `src/prokrustes/cli.py` | CLI, prompt building, HTTP client, extraction and validation |
| `src/prokrustes/template.py` | template scanner and placeholder parsing |
| `src/prokrustes/regexgbnf.py` | Python regex -> GBNF compiler |
| `src/prokrustes/gbnf.py` | GBNF emission: literal escaping, rule names, syntax check |
| `src/prokrustes/unixsocket.py` | HTTP over a Unix domain socket (curl `--unix-socket` style) |
| `tests/gbnf2re.py` | test helper: GBNF back to a Python regex |
| `tests/test_cli.py` | template, grammar and CLI tests |
| `tests/test_regexgbnf.py` | regex compiler tests, including a differential fuzz |
| `tests/test_live_grammar.py` | integration: generated grammars vs a live llama.cpp |

The package is a real package rather than loose top-level modules, so
installing it does not drop a `gbnf` or `template` module into `site-packages`
where it could collide with something else.

## Tests

```bash
python3 -m unittest discover -s tests    # 146 tests, no server needed
```

The unit tests compile each generated grammar back into a Python regex
(`tests/gbnf2re.py`) and check that it accepts exactly what the original pattern
accepts. That verifies the compiler without a model, and for the regex compiler
it runs over hand-written patterns plus a fuzz that generates random ones.

`tests/test_live_grammar.py` is an integration suite that pushes every generated
grammar, plus every bundled example, through a real `llama-server` to confirm
llama.cpp's own parser accepts them. That check matters because a grammar
llama.cpp refuses surfaces at generation time only as "failed to parse grammar",
with no line number. It needs a server and skips without one, so the command
above passes offline:

```bash
PROKRUSTES_LLAMA_URL=http://127.0.0.1:8080 python3 -m unittest discover -s tests
```

Any model will do — grammar compilation does not depend on the weights. One test
sends a deliberately broken grammar as a control, so a server that has stopped
validating cannot make the rest of the suite pass vacuously.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
