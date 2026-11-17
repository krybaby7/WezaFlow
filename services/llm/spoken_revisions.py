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
_EN_DATE_HEAD = rf"(?:(?:on[ \t]+)?(?:{_EN_DAY})|today|tomorrow)"
_EN_CLOCK = rf"at[ \t]+(?:{_EN_TIME})"
_EN_DATE = rf"{_EN_DATE_HEAD}(?:[ \t]+{_EN_CLOCK})?"

_EN_RAW_CUE = r"actually,?[ \t]+make[ \t]+that|make[ \t]+that"
_EN_RAW_QUESTION = r"(?:can|could|would)[ \t]+you[ \t]+(?:confirm|check|let[ \t]+me[ \t]+know)\b"
_EN_RAW_END = rf"(?:(?P<question_gap>[ \t]+)(?={_EN_RAW_QUESTION})|{_END})"
_CLOCK = r"(?:(?:[01٠١]?[0-9٠-٩]|[2٢][0-3٠-٣])(?::[0-5٠-٥][0-9٠-٩])?)"

_EN_CUE = r"(?:actually(?:,?[ \t]+make[ \t]+that)?|no[ \t]+wait|I[ \t]+mean|make[ \t]+that)"
_FR_REQUEST = r"(?:envoie|envoyez|livre|livrez|appelle|appelez|dépose|déposez)"
_FR_DATE = (
    r"(?:aujourd’hui|aujourd'hui|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
    rf"(?:[ \t]+à[ \t]+(?:{_CLOCK}(?:[ \t]+heures?)?|"
    r"(?:une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze)[ \t]+heures?|midi|minuit))?"
)
_AR_REQUEST = r"(?:ابعت|ابعتي|أبعت|سلّم|سلم|كلمني|كلم|نتقابل|هنبعت)"
# Egyptian weekday spellings in the Latin script policy. This table applies
# only to a confirmed replacement span in an otherwise Latin-script sentence.
_AR_WEEKDAYS = {
    "السبت": "el sabt",
    "الأحد": "el 7ad",
    "الاحد": "el 7ad",
    "الحد": "el 7ad",
    "الاثنين": "el etneen",
    "الإثنين": "el etneen",
    "الاتنين": "el etneen",
    "الثلاثاء": "el talat",
    "التلات": "el talat",
    "الأربعاء": "el arba3",
    "الاربعاء": "el arba3",
    "الأربع": "el arba3",
    "الخميس": "el khamees",
    "الجمعة": "el gom3a",
}
_AR_DAY = rf"(?:يوم[ \t]+)?(?:{'|'.join(_AR_WEEKDAYS)})"
_AR_DATE_HEAD = rf"(?:{_AR_DAY}|النهارده|بكرة|بكره)"
_AR_CLOCK = (
    rf"(?:الساعة|الساعه)[ \t]+(?:{_CLOCK}|واحدة|اتنين|تلاتة|أربعة|خمسة|ستة|"
    r"سبعة|تمانية|تسعة|عشرة|حداشر|اتناشر)(?:[ \t]+(?:الصبح|بالليل|الضهر|ظهرًا|مساءً|صباحًا))?"
)
_AR_ANCHORED_DATE = rf"(?:{_AR_DATE_HEAD}|{_EN_DATE_HEAD})(?:[ \t]+(?:{_AR_CLOCK}|{_EN_CLOCK}))?"
_EN_ANCHORED_NEW_DATE = rf"(?:{_AR_DAY}|{_EN_DATE_HEAD})(?:[ \t]+{_EN_CLOCK})?"
_AR_APOLOGY = ("معلش", "معلهش")
_AR_CUE = rf"قصدي|أقصد|اقصد|{'|'.join(_AR_APOLOGY)}"
_AR_WEEKDAY_PREFIX = re.compile(rf"(?:(?P<day_word>يوم)[ \t]+)?(?P<weekday>{'|'.join(_AR_WEEKDAYS)})\b")
_NAME = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]{1,31}"

_QUOTE_OR_CODE = re.compile(r'[“”«»"`]|(?<!\w)[\x27‘’]|[\x27‘’](?!\w)')
_TIME_MARKER = re.compile(r"\b(?:at|à|الساعة|الساعه)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|cannot|non|pas|jamais|"
    r"ما|مش|مات|مت|مفيش|بدون|بلاش)\b|\b(?:ما|مت)\w+ش\b",
    re.IGNORECASE,
)
_EXISTING_DATE_PREPOSITION = re.compile(r"\b(?:for|about|around)$", re.IGNORECASE)
_WEEKDAY_WITH_ON = re.compile(rf"(?:on[ \t]+)?(?=(?:{_EN_DAY})\b)", re.IGNORECASE)
_SENTENCE_BOUNDARY = re.compile(r"[.!?؟](?=\s|\Z)")
_TERMINAL_MODIFIER = re.compile(
    r"\b(?:by|until|before|after|from|between|since|during|through|at|of|with|in|to|on|and|or|avant|après|jusqu|et|ou|"
    r"قبل|بعد|من|لحد|بين|أو|و)$",
    re.IGNORECASE,
)
_CUES = re.compile(
    rf"(?<=[.,،])[ \t]*(?:{_EN_CUE}|sorry|pardon|je[ \t]+veux[ \t]+dire|{_AR_CUE})\b|(?<!\S)(?:{_EN_RAW_CUE})\b",
    re.IGNORECASE,
)


def _deadline_pattern(
    request: str,
    dates: str,
    cue: str,
    *,
    replacement_dates: str | None = None,
    separator: str = r"[.,،][ \t]+",
    end: str = _END,
) -> re.Pattern:
    return re.compile(
        rf"{_START}(?P<prefix>{request}{_ARGUMENTS})[ \t]+"
        rf"(?P<old>{dates})(?P<gap>{separator})(?P<cue>{cue})"
        rf"[,،]?[ \t]+(?P<new>{replacement_dates or dates}){end}",
        re.IGNORECASE,
    )


_DEADLINE_PATTERNS = (
    ("en", _deadline_pattern(
        _EN_REQUEST, _AR_ANCHORED_DATE, rf"{_EN_CUE}|sorry",
        replacement_dates=_EN_ANCHORED_NEW_DATE,
    ), _AR_ANCHORED_DATE),
    ("en", _deadline_pattern(
        _EN_REQUEST, _EN_DATE, _EN_RAW_CUE, separator=r"[ \t]+", end=_EN_RAW_END,
    ), _EN_DATE),
    ("fr", _deadline_pattern(_FR_REQUEST, _FR_DATE, r"pardon|je[ \t]+veux[ \t]+dire"), _FR_DATE),
    ("ar", _deadline_pattern(_AR_REQUEST, _AR_ANCHORED_DATE, _AR_CUE), _AR_ANCHORED_DATE),
)
_RECIPIENT = re.compile(
    rf"{_START}(?P<prefix>{_EN_REQUEST}{_ARGUMENTS})[ \t]+"
    rf"(?i:to)[ \t]+(?P<old>{_NAME}),[ \t]+(?i:sorry),?[ \t]+"
    rf"(?P<new>(?i:to)[ \t]+{_NAME}){_END}"
)


def _needs_arabic_transliteration(text: str) -> bool:
    for sentence in _SENTENCE_BOUNDARY.split(text):
        letter_scripts = [unicodedata.name(char, "") for char in sentence if char.isalpha()]
        if letter_scripts and "LATIN" in letter_scripts[0]:
            if any("ARABIC" in name for name in letter_scripts):
                return True
    return False


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
            not char.isascii() and "LATIN" not in unicodedata.name(char, "")
            for char in prefix if not char.isspace()
        ):
            continue
        if dates and re.search(rf"\b(?:{dates})\b", prefix, re.IGNORECASE):
            continue
        if dates is None and re.search(r"\bto\b", prefix, re.IGNORECASE):
            continue

        replacement = match["new"]
        if dates and _TIME_MARKER.search(match["old"]) and not _TIME_MARKER.search(replacement):
            continue  # A date-only correction can leave the original time in force.
        if language == "ar" and match["cue"] in _AR_APOLOGY and match["gap"].startswith("."):
            if not _TIME_MARKER.search(replacement):
                continue  # A standalone apology and bare weekday may be unrelated.
        if language == "en" and dates:
            arabic_weekday = _AR_WEEKDAY_PREFIX.match(replacement)
            if match["cue"].casefold() == "sorry" and (
                match["gap"].startswith(".") or arabic_weekday is None
            ):
                continue  # An apology alone does not reliably mark a new English deadline.
            if arabic_weekday is not None:
                weekday = _AR_WEEKDAYS[arabic_weekday["weekday"]]
                day_word = "yom " if arabic_weekday["day_word"] else ""
                replacement = day_word + weekday + replacement[arabic_weekday.end():]
            elif (weekday_start := _WEEKDAY_WITH_ON.match(replacement)) is not None:
                if _EXISTING_DATE_PREPOSITION.search(prefix):
                    replacement = replacement[weekday_start.end():]
                elif weekday_start.end() == 0:
                    replacement = "on " + replacement
        trailing = raw[match.end():]
        if match.groupdict().get("question_gap"):
            trailing = "." + match["question_gap"] + trailing
        preceding = raw[:match.start()]
        if _needs_arabic_transliteration(preceding) or _needs_arabic_transliteration(trailing):
            continue
        return preceding + prefix + " " + replacement + trailing
    return None
