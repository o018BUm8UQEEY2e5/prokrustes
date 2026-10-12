"""Fill a text template, constraining a llama-server with a GBNF grammar.

Named for the Procrustes of Greek myth -- commonly spelled Prokrustes, as here
-- who made travellers fit his bed by cutting the tall and tying down the short.
"Procrustean" now means forcing something into a fixed shape regardless of what
it wanted to be. That is the mechanism here: the model supplies the content, the
grammar supplies the bed, and the output conforms to the shape rather than
merely looking like it does.

The command-line entry point is :func:`prokrustes.cli.main`, installed as the
``prokrustes`` script and also reachable as ``python -m prokrustes``.
"""

from .gbnf import GrammarError
from .regexgbnf import RegexSyntaxError
from .template import Field, TemplateError
from .cli import (
    build_chat_messages,
    build_prompt,
    compile_template,
    extract,
    rebuild,
    split_thinking,
    validate,
)

__all__ = [
    "Field",
    "GrammarError",
    "RegexSyntaxError",
    "TemplateError",
    "build_chat_messages",
    "build_prompt",
    "compile_template",
    "extract",
    "rebuild",
    "split_thinking",
    "validate",
]

__version__ = "0.1.0"