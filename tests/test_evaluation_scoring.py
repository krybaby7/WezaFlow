import unittest
from services.evaluation.scoring import score_transcription, transcript_words, score_final

class ScoringTests(unittest.TestCase):
    def test_unicode_and_punctuation(self):
        assert transcript_words("L’été, déjà! مرحبا") == ["l'été", "déjà", "مرحبا"]
        assert score_transcription("été", "e\u0301te\u0301").word_error_rate == 0
        assert score_transcription("été", "ete").word_error_rate == 1
        assert score_transcription("مرحبا", "وداعا").word_error_rate == 1
        assert score_transcription("l'été", "l’été").word_error_rate == 0


    def test_denominators_and_insertions(self):
        score = score_transcription("a", "a b c")
        assert score.word_errors == 2 and score.reference_words == 1
        assert score.word_error_rate == 2
        assert score.word_accuracy == -1
        empty = score_transcription("", "hello")
        assert empty.word_errors == 1 and empty.reference_words == 0
        assert empty.word_error_rate is None


    def test_final_alternatives_and_protected_literals(self):
        result = score_final(["Send AB-12", "Please send AB-12"], "Please send ab-12", [{"text": "AB-12", "category": "code"}])
        assert result["normalized_exact"] is True
        assert result["exact_match"] is False
        assert result["protected_literals"][0]["present_exact"] is False
        assert result["semantic_review"] is None
        assert score_final(["Send 12", "Please send 12"], "Please send 12")["exact_match"] is True
        assert score_final(["Send 12"], "Send 21", [{"text": "12", "category": "number"}])["protected_literals"][0]["present_exact"] is False

    def test_protected_literals_require_whole_occurrences(self):
        assert not score_final(["42"], "142", [{"text": "42", "category": "number"}])["protected_literals"][0]["present_exact"]
        assert score_final(["42"], "(42)", [{"text": "42", "category": "number"}])["protected_literals"][0]["present_exact"]
