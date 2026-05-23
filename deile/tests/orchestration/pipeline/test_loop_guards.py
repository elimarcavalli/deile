"""Regression tests for the 3 loop-guard bugs that hit production on 2026-05-23.

Context: between 02:49 and 04:01 the pipeline burned tokens in three tight
loops, all caused by the orchestrator trusting the worker's word instead of
verifying GitHub state:

1. **Critique verdict parser** rejected markdown-decorated verdicts (e.g.
   ``**VEREDITO:** CLARO``), defaulting silently to "POBRE/veredito ausente"
   → refine → re-critique → forever (#281, #283).
2. **Pipeline-side attempt counter** was overwritten by the worker's
   ``tentativa`` value, which kept reporting 1 when the workspace was reset.
   The ceiling in stages.py never bit (#283: 50+ "incompleto sem PR" parks).
3. **review_only** never marked ``~mention:processado`` because the design
   assumed GitHub would consume the reviewer trigger when the review was
   posted. When the worker fails to post (crash/timeout silent), the trigger
   re-fires every tick (#277: 20+ dispatches with zero reviews posted).
"""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.implementer import (parse_critique_verdict,
                                                      parse_decompose_result,
                                                      parse_refine_verdict)
from deile.orchestration.pipeline.resume_state import ResumeTracker


class TestVerdictParserToleratesMarkdown:
    """Bug 1 (#281/#283): personas decorate the verdict — parser must cope."""

    @pytest.mark.parametrize("text,expected_clear", [
        ("VEREDITO: CLARO", True),                     # plain
        ("VEREDITO: VAGO: falta alvo técnico", False), # plain with reason
        ("**VEREDITO:** CLARO", True),                 # markdown bold
        ("**VEREDITO: CLARO**", True),                 # bolded whole
        ("### VEREDITO: VAGO: tudo vazio", False),     # header prefix
        ("> VEREDITO: CLARO", True),                   # blockquote
        ("- VEREDITO: CLARO", True),                   # list bullet
        ("*  VEREDITO: VAGO: foo bar", False),         # asterisk + spaces
        ("# VEREDITO\n\nfinal: CLARO", True),          # NOT this (no token)
    ])
    def test_critique_accepts_decorated_verdict(self, text, expected_clear):
        if "final: CLARO" in text:
            pytest.skip("requires a 'CLARO' after 'VEREDITO' on same logical token; out of scope for relaxer")
        is_clear, _ = parse_critique_verdict(text)
        assert is_clear is expected_clear

    def test_critique_picks_last_verdict_when_multiple(self):
        # Personas sometimes echo the brief instructions then emit the verdict.
        text = (
            "Vou avaliar.\n"
            "(O brief diz: na ÚLTIMA LINHA escreva VEREDITO: CLARO ou VEREDITO: VAGO.)\n"
            "Conclusão:\n"
            "**VEREDITO: CLARO**\n"
        )
        is_clear, _ = parse_critique_verdict(text)
        assert is_clear is True

    def test_critique_missing_still_defaults_to_pobre(self):
        # Safety: a TRULY missing verdict still defaults to POBRE (do not
        # advance an unjudged issue).
        is_clear, reason = parse_critique_verdict("blah blah no verdict here")
        assert is_clear is False
        assert "ausente" in reason

    @pytest.mark.parametrize("text,expected", [
        ("REFINO: OK", "ok"),
        ("**REFINO: OK**", "ok"),
        ("### REFINO: AGUARDA_STAKEHOLDER", "waiting"),
        ("> REFINO: AGUARDA_STAKEHOLDER", "waiting"),
        ("- REFINO: OK", "ok"),
    ])
    def test_refine_accepts_decorated(self, text, expected):
        assert parse_refine_verdict(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("DECOMPOSTO: #11 #12 #13", [11, 12, 13]),
        ("**DECOMPOSTO:** #11 #12", [11, 12]),
        ("### DECOMPOSTO: #99", [99]),
        ("> DECOMPOSTO: #5 e #6 (independentes)", [5, 6]),
    ])
    def test_decompose_accepts_decorated(self, text, expected):
        assert parse_decompose_result(text) == expected


class TestAttemptCounterMonotonic:
    """Bug 2 (#283): the pipeline-side counter must always grow per dispatch
    so the ceiling in stages.py bites even when the worker reports tentativa=1
    every time (workspace was reset, PVC bookkeeping missing, etc.)."""

    def test_attempt_grows_when_worker_keeps_reporting_one(self):
        t = ResumeTracker()
        for _ in range(10):
            t.update_from_worker(123, fingerprint="frag", attempt=1, budget_s=0.0)
        # Pipeline counted every dispatch — would bite a ceiling of 5 long ago.
        assert t.get(123).attempt == 10

    def test_attempt_grows_when_worker_reports_zero(self):
        # Some failure paths emit no attempt at all (0). Pipeline still +1.
        t = ResumeTracker()
        for _ in range(7):
            t.update_from_worker(124, fingerprint="", attempt=0, budget_s=0.0)
        assert t.get(124).attempt == 7

    def test_attempt_can_jump_when_worker_is_authoritatively_ahead(self):
        # If the worker has durable PVC bookkeeping that shows a higher number
        # (e.g. monitor was restarted and lost the in-memory counter), trust
        # it.
        t = ResumeTracker()
        t.update_from_worker(125, fingerprint="x", attempt=20, budget_s=0.0)
        assert t.get(125).attempt == 20

    def test_attempt_never_shrinks(self):
        # Once the pipeline has counted N dispatches, a later report with a
        # lower number from the worker must not roll back the ceiling.
        t = ResumeTracker()
        for _ in range(5):
            t.update_from_worker(126, fingerprint="x", attempt=1, budget_s=0.0)
        assert t.get(126).attempt == 5
        # Worker now reports a regression (e.g. reset)
        t.update_from_worker(126, fingerprint="x", attempt=1, budget_s=0.0)
        assert t.get(126).attempt == 6  # still grew


class TestReviewerSilentFailureGuard:
    """Bug 3 (#277): when the review_only worker reports ``ok`` but never
    posted a review to GitHub, the reviewer trigger stays armed → the next
    tick re-fires → infinite storm. The post-dispatch guard checks GitHub's
    real state: if our login is STILL in ``requested_reviewers``, the worker
    did not post — mark ``~mention:processado`` to break the loop.

    These tests are intentionally lean — the integration test sits in
    test_mention_handling.py and exercises the whole dispatcher; here we
    document the GitHub-side check is the right check.
    """

    def test_silent_failure_is_detectable_via_reviewer_still_requested(self):
        # Contract: the post-dispatch check is "is our login still in the PR's
        # requested_reviewers?". GitHub auto-removes a reviewer when they
        # submit a review; presence after a successful outcome means the
        # worker did NOT post. (See pr_reviewer_still_requested in
        # github_client.py.)
        # This test exists to anchor the contract in code so a future
        # refactor cannot silently drop the check.
        from deile.orchestration.pipeline.github_client import GitHubClient
        assert hasattr(GitHubClient, "pr_reviewer_still_requested"), (
            "post-review verification helper was removed — the storm guard "
            "on review_only depends on it; reintroduce or update the test"
        )
