from __future__ import annotations

import re
import unicodedata
from typing import TypedDict


MAX_APPROVED_ALIASES = 128
MAX_ALIAS_LENGTH = 128
_MAX_POSTPASS_CHARS = 100_000


class ApprovedAlias(TypedDict):
    spoken: str
    canonical: str


_PROTECTED_TEXT = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`"
    r"|(?<![\w+.-])[\w+.-]+://[^\s<>]+|(?:mailto|tel):[^\s<>]+"
    r"|(?<![\w-])(?:localhost|[\w-]+(?:\.[\w-]+)+)(?::[0-9]+)?[/#?][^\s<>]*"
    r"|[#@$][\w.-]+|(?<![\w.+-])[\w.+-]+@[\w.-]+"
    r"|(?<!\w)\w+(?:(?:::|[./\\-])\w+)+|[./\\]\w+",
    re.IGNORECASE,
)


def substitute_aliases(text: str, aliases: list[ApprovedAlias]) -> str:
    if not aliases or len(text) > _MAX_POSTPASS_CHARS:
        return text
    replacements = {alias["spoken"].casefold(): alias["canonical"] for alias in aliases}
    alternatives = sorted(replacements, key=lambda key: (-len(key), key))
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, alternatives)) + r")(?!\w)")

    result_length = len(text)

    def replace_in_prose(prose: str) -> str:
        nonlocal result_length
        folded = prose.casefold()
        # A full case fold can expand one code point into several. Map only
        # real boundaries so a match cannot split an original character.
        offsets = range(len(prose) + 1)
        if len(folded) != len(prose):
            boundaries = {}
            offset = 0
            for index, character in enumerate(prose):
                boundaries[offset] = index
                offset += len(character.casefold())
            boundaries[offset] = len(prose)
            offsets = boundaries

        pieces = []
        cursor = 0
        for match in pattern.finditer(folded):
            try:
                start, end = offsets[match.start()], offsets[match.end()]
            except KeyError:
                continue
            # Python's Unicode word class excludes combining marks.
            if (start and unicodedata.category(prose[start - 1]).startswith("M")
                or end < len(prose) and unicodedata.category(prose[end]).startswith("M")):
                continue
            replacement = replacements[match.group()]
            result_length += len(replacement) - (end - start)
            pieces.extend((prose[cursor:start], replacement))
            cursor = end
        return "".join((*pieces, prose[cursor:]))

    pieces = []
    cursor = 0
    for protected in _PROTECTED_TEXT.finditer(text):
        pieces.extend((replace_in_prose(text[cursor:protected.start()]), protected.group()))
        cursor = protected.end()
    pieces.append(replace_in_prose(text[cursor:]))
    return "".join(pieces) if result_length <= _MAX_POSTPASS_CHARS else text


def starts_with_canonical_name(text: str, canonical_names: list[str]) -> bool:
    text = text.lstrip()
    for name in canonical_names:
        if not text.startswith(name):
            continue
        end = len(name)
        if end == len(text):
            return True
        next_character = text[end]
        if (
            next_character.isalnum()
            or next_character == "_"
            or unicodedata.category(next_character).startswith("M")
        ):
            continue
        # Check whether the last word continues into an identifier, URL, or email.
        last_word = name.rsplit(maxsplit=1)[-1]
        protected = _PROTECTED_TEXT.match(text, end - len(last_word))
        if protected is None or protected.end() <= end:
            return True
    return False
