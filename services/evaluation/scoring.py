"""Unicode-preserving edit metrics. Rates are undefined for nonempty/empty refs."""
from dataclasses import asdict, dataclass
import unicodedata
import re
from typing import Any, Sequence


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold().replace("\u2019", "'")
    return " ".join("".join(c if c == "'" or not unicodedata.category(c).startswith("P") else " " for c in text).split())


def transcript_words(text: str) -> list[str]:
    return normalize(text).split()


def _distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        next_row = [i]
        for j, y in enumerate(b, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j-1] + (x != y)))
        row = next_row
    return row[-1]


@dataclass(frozen=True)
class TranscriptionScore:
    word_errors: int
    reference_words: int
    character_errors: int
    reference_characters: int
    word_error_rate: float | None
    character_error_rate: float | None
    word_accuracy: float | None

    def to_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


def score_transcription(expected_text: str, actual_text: str) -> TranscriptionScore:
    ref, hyp = normalize(expected_text), normalize(actual_text)
    words, chars = len(ref.split()), len(ref)
    we, ce = _distance(ref.split(), hyp.split()), _distance(ref, hyp)
    wer = we / words if words else (0.0 if not we else None)
    cer = ce / chars if chars else (0.0 if not ce else None)
    return TranscriptionScore(we, words, ce, chars, wer, cer, None if wer is None else 1 - wer)


def score_final(
    alternatives: Sequence[str],
    actual: str,
    protected_literals: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    scores = [score_transcription(ref, actual) for ref in alternatives]
    best = min(range(len(scores)), key=lambda i: (scores[i].word_errors, scores[i].character_errors))
    return {"best_alternative_index": best, "score": scores[best].to_dict(),
            "exact_match": actual in alternatives,
            "normalized_exact": any(normalize(actual) == normalize(ref) for ref in alternatives),
            "protected_literals": [{**item, "present_exact": re.search(r"(?<!\w)" + re.escape(item["text"]) + r"(?!\w)", actual) is not None} for item in protected_literals],
            "semantic_review": None, "grammar_review": None}
