"""Resolve a single explicitly signaled deadline or recipient change."""

import re
import unicodedata


# Anchor the original request at a sentence boundary and recognize only simple
# affirmative request shapes. A compatible replacement must end its own clause.
_START = r"(?<!\S)(?:(?<=[.!?؟][ \t])|\A)"
_END = r"(?=(?:[.!?؟](?=\s|\Z)|\Z))"
_WORD = r"[^\W_]+"
_ARGUMENTS = rf"(?:[ \t]+{_WORD}){{0,16}}?"
_EN_REQUEST = (
    r"(?i:(?:please[ \t]+|I[ \t]+(?:need|want|have)[ \t]+to[ \t]+)?"
    r"(?:send|email|submit|meet|call|deliver|finish|schedule))"
)
_EN_DAY = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_EN_TIME = (
    r"(?:[01]?[0-9]|2[0-3])(?::[0-5][0-9])?(?:[ \t]*(?:am|pm))?"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midnight"
)
_EN_DATE = (
    rf"(?:(?:on[ \t]+)?(?:{_EN_DAY})|today|tomorrow)"
    rf"(?:[ \t]+at[ \t]+(?:{_EN_TIME}))?"
)
_EN_CUE = r"(?:actually(?:,?[ \t]+make[ \t]+that)?|no[ \t]+wait|I[ \t]+mean|make[ \t]+that)"
_FR_REQUEST = r"(?:envoie|envoyez|livre|livrez|appelle|appelez|dépose|déposez)"
_FR_DATE = r"(?:aujourd’hui|aujourd'hui|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
_AR_REQUEST = r"(?:ابعت|ابعتي|أبعت|سلّم|سلم|كلمني|كلم|نتقابل|هنبعت)"
_AR_DATE = r"(?:النهارده|بكرة|بكره|السبت|الأحد|الاحد|الحد|الاثنين|الإثنين|الاتنين|الثلاثاء|التلات|الأربعاء|الاربعاء|الأربع|الخميس|الجمعة)"
_NAME = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]{1,31}"

_QUOTE_OR_CODE = re.compile(r'[“”«»"`]|(?<!\w)[\x27‘’]|[\x27‘’](?!\w)')
_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|cannot|non|pas|jamais|"
    r"ما|مش|مات|مت|مفيش|بدون|بلاش)\b|\b(?:ما|مت)\w+ش\b",
    re.IGNORECASE,
)
_TERMINAL_MODIFIER = re.compile(
    r"\b(?:by|until|before|after|from|between|since|and|or|avant|après|jusqu|et|ou|"
    r"قبل|بعد|من|لحد|بين|أو|و)$",
    re.IGNORECASE,
)
_CUES = re.compile(
    rf"(?<=[.,،])[ \t]*(?:{_EN_CUE}|sorry|pardon|je[ \t]+veux[ \t]+dire|قصدي|أقصد|اقصد)\b",
    re.IGNORECASE,
)


def _deadline_pattern(request: str, dates: str, cue: str) -> re.Pattern:
    return re.compile(
        rf"{_START}(?P<prefix>{request}{_ARGUMENTS})[ \t]+"
        rf"(?P<old>{dates})[.,،][ \t]+(?:{cue})"
        rf"[,،]?[ \t]+(?P<new>{dates}){_END}",
        re.IGNORECASE,
    )


_DEADLINE_PATTERNS = (
    ("en", _deadline_pattern(_EN_REQUEST, _EN_DATE, _EN_CUE), _EN_DATE),
    ("fr", _deadline_pattern(_FR_REQUEST, _FR_DATE, r"pardon|je[ \t]+veux[ \t]+dire"), _FR_DATE),
    ("ar", _deadline_pattern(_AR_REQUEST, _AR_DATE, r"قصدي|أقصد|اقصد"), _AR_DATE),
)
_RECIPIENT = re.compile(
    rf"{_START}(?P<prefix>{_EN_REQUEST}{_ARGUMENTS})[ \t]+"
    rf"(?i:to)[ \t]+(?P<old>{_NAME}),[ \t]+(?i:sorry),?[ \t]+"
    rf"(?P<new>(?i:to)[ \t]+{_NAME}){_END}"
)


def resolve_spoken_revision(raw: str) -> str | None:
    """Replace a supported span while keeping identical text on both sides."""
    if not isinstance(raw, str) or len(raw) > 2_000 or _QUOTE_OR_CODE.search(raw):
        return None
    if "\0" in raw or any("\ud800" <= char <= "\udfff" for char in raw):
        return None
    if len(list(_CUES.finditer(raw))) != 1:
        return None

    for language, pattern, dates in (*_DEADLINE_PATTERNS, ("en", _RECIPIENT, None)):
        match = pattern.search(raw)
        if match is None:
            continue
        prefix = match["prefix"]
        if _NEGATION.search(prefix) or _TERMINAL_MODIFIER.search(prefix):
            continue
        if language != "ar" and any(
            char.isalpha() and "ARABIC" in unicodedata.name(char, "") for char in prefix
        ):
            continue
        if dates and re.search(rf"\b(?:{dates})\b", prefix, re.IGNORECASE):
            continue
        if dates is None and re.search(r"\bto\b", prefix, re.IGNORECASE):
            continue

        replacement = match["new"]
        if language == "en" and dates and re.match(rf"\b(?:{_EN_DAY})\b", replacement, re.IGNORECASE):
            replacement = "on " + replacement
        return raw[:match.end("prefix")] + " " + replacement + raw[match.end():]
    return None
