from app.agent.prompts import FAST_SYSTEM_PROMPT, SYSTEM_PROMPT


def test_prompts_forbid_unsupported_absence_claims():
    assert "does not exist" in FAST_SYSTEM_PROMPT and "not checked" in FAST_SYSTEM_PROMPT
    assert "Absence and completeness" in SYSTEM_PROMPT and "I did not find" in SYSTEM_PROMPT


def test_prompts_keep_core_contract():
    # the JSON action format and the citation rule must survive prompt edits
    for p in (FAST_SYSTEM_PROMPT, SYSTEM_PROMPT):
        assert "[path:start-end]" in p and "untrusted_code" in p and "final_answer" in p


def test_fast_prompt_demands_citations_and_shows_an_example():
    assert "EVERY sentence" in FAST_SYSTEM_PROMPT and "rejected" in FAST_SYSTEM_PROMPT
    assert '"answer": "`login`' in FAST_SYSTEM_PROMPT and "[app/auth.py:12-30]" in FAST_SYSTEM_PROMPT
