"""Change a committed JSON file's top-level values without reformatting it.

The CARE release file is laid out by people — its key order, its indentation,
the blank lines inside an empty list, whether it ends in a newline — and a
release is reviewed as a diff of it. Writing the document back out with
``json.dumps`` rewrites every one of those, buries the real change in noise and
escapes any non-ASCII prose. So only the value spans that change are replaced
and every other byte is copied through.

No regex (repo rule): keys are read with ``json.decoder.scanstring`` and values
are measured with ``JSONDecoder.raw_decode`` — the scanners ``json.loads``
itself uses — so a span ends exactly where the parser says the value ends.
"""
from __future__ import annotations

import json
from json.decoder import scanstring
from typing import Any

_WS = " \t\n\r"


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _expect(text: str, i: int, char: str, what: str) -> int:
    if i >= len(text) or text[i] != char:
        raise ValueError(f"expected {what} at offset {i}")
    return i + 1


def _line_indent(text: str, i: int) -> str:
    """Leading whitespace of the line holding offset ``i``."""
    start = text.rfind("\n", 0, i) + 1
    end = start
    while end < i and text[end] in " \t":
        end += 1
    return text[start:end]


def top_level_spans(text: str) -> dict[str, tuple[int, int, str]]:
    """``{key: (value_start, value_end, indent of the key's line)}`` for the
    members of the top-level object — never a nested one."""
    decoder = json.JSONDecoder()
    spans: dict[str, tuple[int, int, str]] = {}
    i = _skip_ws(text, _expect(text, _skip_ws(text, 0), "{", "'{'"))
    if i < len(text) and text[i] == "}":
        return spans
    while True:
        key_at = i
        key, i = scanstring(text, _expect(text, i, '"', "a key"))
        i = _skip_ws(text, _expect(text, _skip_ws(text, i), ":", "':'"))
        _, end = decoder.raw_decode(text, i)
        # json.loads keeps the LAST of two equal keys; editing the first would
        # change bytes that do not count, so a duplicate is refused outright.
        if key in spans:
            raise ValueError(f"duplicate key {key!r}")
        spans[key] = (i, end, _line_indent(text, key_at))
        i = _skip_ws(text, end)
        if i < len(text) and text[i] == ",":
            i = _skip_ws(text, i + 1)
            continue
        _expect(text, i, "}", "',' or '}'")
        return spans


def _canon(value: Any) -> str:
    # true, 1 and 1.0 compare equal in Python but are different JSON.
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _render(value: Any, indent: str, newline: str) -> str:
    if isinstance(value, list) and value:
        inner = newline + indent + "  "
        return "[" + ",".join(inner + json.dumps(v, ensure_ascii=False) for v in value) \
            + newline + indent + "]"
    return json.dumps(value, ensure_ascii=False)


def splice_top_level(text: str, updates: dict[str, Any]) -> str:
    """``text`` with the top-level values in ``updates`` replaced in place.

    Raises ValueError — and changes nothing — when ``text`` is not a JSON
    object, or when ``updates`` names a key the file does not already have:
    the file's owners decide its shape, so a key is never added.
    """
    try:
        original = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"not valid JSON: {e}") from None
    if not isinstance(original, dict):
        raise ValueError("not a JSON object")
    spans = top_level_spans(text)
    unknown = sorted(k for k in updates if k not in spans)
    if unknown:
        raise ValueError(f"not in the file, so never added: {', '.join(unknown)}")
    newline = "\r\n" if "\r\n" in text else "\n"
    try:
        edits = sorted(
            ((spans[key][0], spans[key][1], _render(value, spans[key][2], newline))
             for key, value in updates.items()
             # An unchanged value keeps its bytes — an empty list spread over
             # blank lines stays exactly as its owner left it.
             if _canon(original[key]) != _canon(value)),
            reverse=True,
        )
        expected = _canon({**original, **updates})
    except TypeError as e:
        raise ValueError(f"a value is not JSON: {e}") from None
    out = text
    for start, end, rendered in edits:       # right to left: earlier offsets stay valid
        out = out[:start] + rendered + out[end:]
    if _canon(json.loads(out)) != expected:
        raise ValueError("the edited text does not read back as the intended document")
    return out
