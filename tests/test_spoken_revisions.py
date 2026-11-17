import json
import unittest
from types import SimpleNamespace

from services.llm.constrained_editor import ConstrainedEditor
from services.runtime.local_editor import LocalCleanupFormatter
from services.pipeline.models import AppContext, AsrResult
from services.pipeline.orchestrator import PipelineOrchestrator


FRIDAY = (
    "I need to send the report tomorrow. Actually, make that Friday at 10. "
    "Can you confirm whether Adam will attend?"
)
FRIDAY_EDITED = (
    "I need to send the report on Friday at 10. Can you confirm whether Adam will attend?"
)


class RecordingBackend:
    def __init__(self, failure=None):
        self.failure = failure
        self.requests = []

    def edit(self, *, system_prompt, user_payload):
        self.requests.append(json.loads(user_payload))
        if self.failure == "timeout":
            raise TimeoutError
        if self.failure == "malformed":
            return '{"edited_text": '
        return json.dumps({"edited_text": self.requests[-1]["raw"]})


class SpokenRevisionsTests(unittest.TestCase):
    def test_unmistakable_revisions_preserve_surrounding_text(self):
        fixtures = (
            ("filed Friday example", FRIDAY, FRIDAY_EDITED),
            ("surrounding sentences",
             "Please read this. " + FRIDAY, "Please read this. " + FRIDAY_EDITED),
            ("actual ASR output with punctuation only after cue",
             "I need to send the report tomorrow Actually, make that Friday at 10. "
             "Can you confirm whether Adam will attend?",
             FRIDAY_EDITED),
            ("unpunctuated raw",
             "i need to send the report tomorrow actually make that friday at 10 can you confirm whether Adam will attend",
             "i need to send the report on friday at 10. can you confirm whether Adam will attend"),
            ("existing date preposition",
             "Send the report for tomorrow. Actually, make that Friday.",
             "Send the report for Friday."),
            ("remove duplicate weekday preposition",
             "Send the report about tomorrow. Actually, make that on Friday.",
             "Send the report about Friday."),
            ("carry independent approximation",
             "Send it around tomorrow. Actually, make that on Monday at ten.",
             "Send it around Monday at ten."),
            ("standalone Arabic sentence",
             "Send it tomorrow. Actually, make that Friday. اسأل Adam لو جاي.",
             "Send it on Friday. اسأل Adam لو جاي."),
            ("other weekday",
             "Please submit the invoice today actually make that Tuesday at 9",
             "Please submit the invoice on Tuesday at 9"),
            ("French time",
             "Envoie le rapport demain, pardon jeudi à dix heures. Tu peux confirmer ?",
             "Envoie le rapport jeudi à dix heures. Tu peux confirmer ?"),
            ("Egyptian apology cue",
             "ابعت التقرير بكرة، معلش، يوم الجمعة الساعة عشرة. هل آدم هيحضر؟",
             "ابعت التقرير يوم الجمعة الساعة عشرة. هل آدم هيحضر؟"),
            ("Arabic-first English date",
             "ابعت التقرير بكرة، معلش، Friday at ten. اسأل Adam لو جاي.",
             "ابعت التقرير Friday at ten. اسأل Adam لو جاي."),
            ("Arabic-first mixed time",
             "ابعت التقرير tomorrow، قصدي Friday الساعة عشرة. اسأل Adam هيحضر ولا لأ.",
             "ابعت التقرير Friday الساعة عشرة. اسأل Adam هيحضر ولا لأ."),
            ("English-first Arabic day",
             "Send the report بكرة, sorry, الجمعة at ten. Ask Adam if he is coming.",
             "Send the report el gom3a at ten. Ask Adam if he is coming."),
            ("English-first Arabic day with marker",
             "Send the report tomorrow, I mean, يوم الخميس at nine. Ask Adam if he is coming.",
             "Send the report yom el khamees at nine. Ask Adam if he is coming."),
            ("Egyptian Arabic time",
             "ابعت التقرير بكرة، قصدي الخميس الساعة ١٠. ممكن تأكد؟",
             "ابعت التقرير الخميس الساعة ١٠. ممكن تأكد؟"),
            ("different recipients", "Email it to Sam, sorry, to Marie.", "Email it to Marie."),
            ("recipient", "Send it to Adam, sorry, to Amir.", "Send it to Amir."),
            ("Friday with words", "Send the report tomorrow, actually Friday at ten.",
             "Send the report on Friday at ten."),
            ("French", "Envoie le rapport demain, pardon, vendredi. Tu peux confirmer ?",
             "Envoie le rapport vendredi. Tu peux confirmer ?"),
            ("Egyptian Arabic", "ابعت التقرير بكرة، قصدي الجمعة. ممكن تأكد؟",
             "ابعت التقرير الجمعة. ممكن تأكد؟"),
            ("Arabic first", "ابعت الـ report بكرة، قصدي الجمعة. ممكن تأكد يا Adam؟",
             "ابعت الـ report الجمعة. ممكن تأكد يا Adam؟"),
            ("English first", "Send the report 3ashan Adam tomorrow. Actually, make that Friday.",
             "Send the report 3ashan Adam on Friday."),
        )
        for name, raw, expected in fixtures:
            with self.subTest(case=name):
                backend = RecordingBackend(failure="timeout")
                formatter = LocalCleanupFormatter(ConstrainedEditor(backend))
                result = formatter.format(raw, context=None)
                self.assertEqual(result.text, expected)
                self.assertEqual(result.model, "local-spoken-revision")
                self.assertEqual(formatter.last_diagnostics["reason"], "deterministic_spoken_revision")
                self.assertEqual(backend.requests, [])

    def test_pipeline_retains_raw_transcript_for_recovery(self):
        backend = RecordingBackend(failure="timeout")
        pipeline = PipelineOrchestrator(
            vad=SimpleNamespace(filter=lambda audio: audio),
            transcriber=SimpleNamespace(
                transcribe=lambda audio, **kwargs: AsrResult(FRIDAY, "en", 0),
            ),
            formatter=LocalCleanupFormatter(ConstrainedEditor(backend)),
            context_provider=lambda: AppContext("chat", "", "work_chat"),
            injector=None,
        )
        result = pipeline.process_audio([0.25])
        self.assertEqual(result.raw_transcript, FRIDAY)
        self.assertEqual(result.polished_text, FRIDAY_EDITED)
        self.assertEqual(result.diagnostics["editing"]["reason"], "deterministic_spoken_revision")

    def test_other_shapes_use_existing_editor_with_exact_input(self):
        cases = (
            "Send the report tomorrow and Friday.",
            "The word 'actually' appears in the title.",
            'The quote was "Send the report tomorrow. Actually, make that Friday."',
            "Do not send the report tomorrow. Actually, make that Friday.",
            "Never send the report tomorrow, actually Friday.",
            "I didn't send the report tomorrow. Actually, make that Friday.",
            "Will you send the report tomorrow? Actually, make that Friday.",
            "Envoie pas le rapport demain, pardon vendredi.",
            "متبعتش التقرير بكرة، قصدي الجمعة.",
            "Put the command send tomorrow, actually Friday in the script.",
            "Send `the report tomorrow. Actually, make that Friday.`",
            "Send the report tomorrow. Actually, make that Friday or Monday.",
            "Send it to Adam, sorry, to Amir and Sarah.",
            "Send it to Adam, sorry, to https://Amir.example.",
            "Send the report tomorrow. Actually, make that Friday. Actually, make that Monday.",
            "Send التقرير tomorrow, actually Friday.",
            "I said send the report tomorrow, actually Friday.",
            "Send it on Friday. Actually Friday works for a different report.",
            "Send it on Friday actually Friday works for a different report",
            "Send it tomorrow actually don't send it Friday can you confirm",
            "Send it tomorrow actually make that Friday before sunset",
            "Send it tomorrow at ten. Actually, make that Friday.",
            "Send it tomorrow. Sorry, Friday won't work for the other report.",
            "Send it tomorrow. Sorry, Friday.",
            "Send التقرير بكرة, sorry, الجمعة at ten. Ask Adam.",
            "Send отчёт بكرة, sorry, الجمعة at ten. Ask Adam.",
            "Send the report بكرة. Sorry, الجمعة at ten.",
            "Send the report بكرة, sorry, الجمعة الساعة عشرة. Ask Adam.",
            "ابعت التقرير بكرة. معلش، يوم الجمعة ما ينفعش للاجتماع.",
            "ابعت التقرير بكرة. معلش، الجمعة.",
            "Send the report by tomorrow, actually Friday.",
            "Send it tomorrow. Actually, make that Friday. Ask Adam لو جاي.",
            "Ask Adam لو جاي. Send it tomorrow. Actually, make that Friday.",
            "Send it on Monday then tomorrow. Actually, make that Friday.",
            "Send it to Adam and to Harold, sorry, to Amir.",
            "Send it to Adam, sorry, to Aمير.",
            "Send it tomorrow. Actually, Friday.5 is the invoice label.",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                backend = RecordingBackend()
                formatter = LocalCleanupFormatter(ConstrainedEditor(backend))
                self.assertEqual(formatter.format(raw, context=None).text, raw)
                self.assertEqual(backend.requests[0]["raw"], raw)
                self.assertEqual(formatter.last_diagnostics["reason"], "unchanged")

    def test_disabled_preserves_raw_even_when_revision_is_supported(self):
        formatter = LocalCleanupFormatter()
        self.assertEqual(formatter.format(FRIDAY, context=None).text, FRIDAY)
        self.assertEqual(formatter.last_diagnostics["reason"], "local_editor_disabled")

    def test_unmatched_revisions_keep_model_failure_recovery(self):
        raw = "Send the report tomorrow, or rather after Adam approves it."
        for failure, reason in (("timeout", "backend_exception"), ("malformed", "malformed_json")):
            with self.subTest(failure=failure):
                backend = RecordingBackend(failure=failure)
                formatter = LocalCleanupFormatter(ConstrainedEditor(backend))
                self.assertEqual(formatter.format(raw, context=None).text, raw)
                self.assertEqual(formatter.last_diagnostics["reason"], reason)
                self.assertEqual(backend.requests[0]["raw"], raw)
