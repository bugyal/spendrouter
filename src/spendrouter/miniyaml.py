"""A small, strict YAML-subset parser (stdlib only).

spendrouter reads ``spendrouter.yml`` without third-party dependencies, so it
carries its own parser for the subset a config file needs:

* block mappings and block sequences (indentation with spaces)
* single-line flow collections: ``[a, b]`` and ``{k: v}``
* scalars: ``null``/``~``, ``true``/``false``, ints (``1_000`` allowed),
  floats, plain strings, 'single' and "double" quoted strings
* comments: ``#`` at the start of a line or after whitespace

Anything outside the subset (anchors, aliases, tags, block scalars,
multiple documents, tabs) is rejected with a line number instead of being
misread. A typo in a spend limit should fail loudly, not silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["YAMLError", "loads"]


class YAMLError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


@dataclass
class _Line:
    indent: int
    text: str
    lineno: int


_INT = re.compile(r"^[-+]?[0-9][0-9_]*$")
_FLOAT = re.compile(r"^[-+]?(?:[0-9][0-9_]*\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")
_UNSUPPORTED = {
    "&": "anchors are not supported",
    "*": "aliases are not supported (to mean a literal '*', quote it: \"*\")",
    "!": "tags are not supported",
    "|": "block scalars are not supported; use a quoted string",
    ">": "block scalars are not supported; use a quoted string",
    "%": "directives are not supported",
    "@": "'@' cannot start a plain value; quote it",
    "`": "'`' cannot start a plain value; quote it",
}
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def loads(text: str) -> Any:
    """Parse a YAML-subset document. Mapping keys are always strings."""
    lines = _prepare(text)
    if not lines:
        return None
    value, i = _parse_block(lines, 0, lines[0].indent)
    if i < len(lines):
        raise YAMLError("unexpected content (check the indentation)", lines[i].lineno)
    return value


# -- line preparation --------------------------------------------------------


def _opens_quote(s: str, i: int) -> bool:
    """Quotes only quote at the start of a token, so ``don't`` stays plain."""
    return i == 0 or s[i - 1] in " \t[{,:"


def _strip_comment(s: str) -> str:
    quote = None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and s[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif c in "\"'" and _opens_quote(s, i):
            quote = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
        i += 1
    return s.rstrip()


def _prepare(text: str) -> list[_Line]:
    out: list[_Line] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        body = raw.lstrip(" ")
        if body.startswith("\t") and body.strip():
            raise YAMLError("tabs are not allowed for indentation", lineno)
        stripped = _strip_comment(raw)
        content = stripped.strip()
        if not content:
            continue
        if content == "---":
            if out:
                raise YAMLError("multiple documents are not supported", lineno)
            continue
        if content == "...":
            break
        out.append(_Line(len(stripped) - len(stripped.lstrip(" ")), content, lineno))
    return out


# -- block structure ---------------------------------------------------------


def _is_seq_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _parse_block(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    if _is_seq_item(lines[i].text):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[_Line], i: int, indent: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YAMLError("unexpected indentation", line.lineno)
        if _is_seq_item(line.text):
            raise YAMLError("expected 'key: value' but found a list item", line.lineno)
        key, rest = _split_key(line)
        if key in out:
            raise YAMLError(f"duplicate key {key!r}", line.lineno)
        i += 1
        if rest:
            out[key] = _parse_inline(rest, line.lineno)
        elif i < len(lines) and lines[i].indent > indent:
            out[key], i = _parse_block(lines, i, lines[i].indent)
        elif i < len(lines) and lines[i].indent == indent and _is_seq_item(lines[i].text):
            out[key], i = _parse_seq(lines, i, indent)
        else:
            out[key] = None
    return out, i


def _parse_seq(lines: list[_Line], i: int, indent: int) -> tuple[list, int]:
    out: list = []
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YAMLError("unexpected indentation", line.lineno)
        if not _is_seq_item(line.text):
            break
        rest = line.text[1:].lstrip(" ")
        if not rest:
            i += 1
            if i < len(lines) and lines[i].indent > indent:
                item, i = _parse_block(lines, i, lines[i].indent)
            else:
                item = None
        else:
            child_indent = indent + len(line.text) - len(rest)
            if _is_seq_item(rest) or (rest[0] not in "[{" and _find_key_colon(rest) is not None):
                # "- key: value" opens a mapping whose further keys sit at the
                # column of "key"; re-read this line as that mapping's first line.
                lines[i] = _Line(child_indent, rest, line.lineno)
                item, i = _parse_block(lines, i, child_indent)
            else:
                item = _parse_inline(rest, line.lineno)
                i += 1
        out.append(item)
    return out, i


def _find_key_colon(text: str) -> int | None:
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif c in "\"'" and _opens_quote(text, i):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth = max(0, depth - 1)
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return i
        i += 1
    return None


def _split_key(line: _Line) -> tuple[str, str]:
    idx = _find_key_colon(line.text)
    if idx is None:
        raise YAMLError("expected 'key: value'", line.lineno)
    raw_key = line.text[:idx].strip()
    rest = line.text[idx + 1 :].strip()
    if not raw_key:
        raise YAMLError("empty key", line.lineno)
    if raw_key[0] in "\"'":
        key, end = _read_quoted(raw_key, 0, line.lineno)
        if raw_key[end:].strip():
            raise YAMLError("unexpected text after quoted key", line.lineno)
    else:
        if raw_key[0] in "[{?":
            raise YAMLError("complex keys are not supported", line.lineno)
        key = raw_key
    return key, rest


# -- inline values -----------------------------------------------------------


def _parse_inline(text: str, lineno: int) -> Any:
    if text[0] in "[{":
        return _Flow(text, lineno).parse()
    if text[0] in "\"'":
        value, end = _read_quoted(text, 0, lineno)
        if text[end:].strip():
            raise YAMLError("unexpected text after quoted string", lineno)
        return value
    if text[0] in _UNSUPPORTED:
        raise YAMLError(_UNSUPPORTED[text[0]], lineno)
    return _resolve(text)


def _resolve(s: str) -> Any:
    s = s.strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT.match(s):
        return int(s.replace("_", ""))
    if _FLOAT.match(s):
        return float(s.replace("_", ""))
    return s


def _read_quoted(text: str, i: int, lineno: int) -> tuple[str, int]:
    """Read a quoted string starting at text[i]; return (value, index after it)."""
    quote = text[i]
    i += 1
    out = []
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                if text[i + 1 : i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(c)
            i += 1
            continue
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            nxt = text[i + 1 : i + 2]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u" and re.match(r"[0-9a-fA-F]{4}$", text[i + 2 : i + 6]):
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
                continue
            raise YAMLError(f"unsupported escape '\\{nxt}'", lineno)
        out.append(c)
        i += 1
    raise YAMLError("unterminated quoted string", lineno)


class _Flow:
    """Recursive-descent reader for single-line ``[...]`` / ``{...}`` values."""

    def __init__(self, text: str, lineno: int):
        self.s = text
        self.i = 0
        self.lineno = lineno

    def error(self, message: str) -> YAMLError:
        return YAMLError(message, self.lineno)

    def parse(self) -> Any:
        value = self.value()
        self.ws()
        if self.i != len(self.s):
            raise self.error("unexpected text after flow collection")
        return value

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def value(self) -> Any:
        self.ws()
        c = self.peek()
        if c == "[":
            return self.seq()
        if c == "{":
            return self.map()
        if c in "\"'" and c:
            value, self.i = _read_quoted(self.s, self.i, self.lineno)
            return value
        if c in _UNSUPPORTED:
            raise self.error(_UNSUPPORTED[c])
        return _resolve(self.plain(",]}"))

    def plain(self, stops: str) -> str:
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in stops:
            if self.s[self.i] in "[{":
                raise self.error(f"unexpected {self.s[self.i]!r} inside a flow value")
            self.i += 1
        return self.s[start : self.i].strip()

    def seq(self) -> list:
        self.i += 1
        out = []
        while True:
            self.ws()
            if self.peek() == "]":
                self.i += 1
                return out
            if not self.peek():
                raise self.error("unterminated '['")
            out.append(self.value())
            self.ws()
            if not self.peek():
                raise self.error("unterminated '['")
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                raise self.error("expected ',' or ']'")

    def map(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.i += 1
                return out
            if not self.peek():
                raise self.error("unterminated '{'")
            if self.peek() in "\"'":
                key, self.i = _read_quoted(self.s, self.i, self.lineno)
            else:
                key = self.plain(":,}")
            self.ws()
            if self.peek() != ":":
                raise self.error(f"expected ':' after key {key!r}")
            self.i += 1
            self.ws()
            value = None if self.peek() in (",", "}") else self.value()
            if key in out:
                raise self.error(f"duplicate key {key!r}")
            out[key] = value
            self.ws()
            if not self.peek():
                raise self.error("unterminated '{'")
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "}":
                raise self.error("expected ',' or '}'")
