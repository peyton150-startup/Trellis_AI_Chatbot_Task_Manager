"""D-73 model-facing task-reference resolution contract.

The prompt must express exactly one way to decide which task a reference means,
and that way is to read the `resolved` field. A second mechanism phrased in terms
of how many candidates came back would contradict domain semantics the moment a
unique exact title outranks weaker substring matches, because that result
legitimately carries several candidates and a decided task at the same time.
"""

from app.prompts import SYSTEM_PROMPT


def _normalized() -> str:
    return " ".join(SYSTEM_PROMPT.split()).lower()


def test_the_prompt_routes_identity_through_deterministic_resolution():
    prompt = _normalized()

    required = (
        # Known ids are reused rather than rediscovered.
        "canonical trellis history",
        "reuse that id directly",
        # The resolver is the discovery boundary.
        "resolve_task_reference",
        # The decision is read, not recomputed.
        "trellis decides which task the reference means",
        "read the resolved field",
        "resolved.task_id",
        "resolved is null and candidates is empty",
        "resolved is null and candidates is not empty",
        "do not guess",
        "ask the user which candidate they mean",
        # Mutation preconditions come from the resolved task.
        "resolved.exists_now=true",
        "resolved.current_version as expected_version",
        # Deleted tasks are readable but never mutable.
        "exists_now=false",
        "get_task_history",
        "never be a mutation target",
        # list_tasks keeps its collection role and loses its resolver role.
        "use list_tasks for browsing or filtering task sets",
    )

    missing = [fragment for fragment in required if fragment not in prompt]

    assert not missing, (
        "resolver prompt contract incomplete; missing fragments: " f"{missing}"
    )


def test_the_prompt_carries_no_second_identity_mechanism():
    prompt = _normalized()

    # Each of these was true before D-73 and is now actively wrong. They are
    # asserted absent rather than merely replaced, because the failure mode is a
    # later edit reintroducing one of them alongside the resolved contract and
    # leaving the model with two rules that disagree.
    forbidden = {
        "do not invent a historical title-search capability": (
            "D-73 introduces exactly that capability"
        ),
        "require exactly one current candidate": (
            "a unique exact title resolves even beside weaker substring matches"
        ),
        "one candidate: use that candidate": "candidate counting is not the rule",
        "multiple candidates: ask the user to clarify": (
            "multiple candidates can still carry a resolved task"
        ),
        "exact version returned by list_tasks": (
            "expected_version comes from resolved.current_version"
        ),
    }

    present = {
        fragment: reason
        for fragment, reason in forbidden.items()
        if fragment in prompt
    }

    assert not present, f"stale identity language survived in the prompt: {present}"


def test_the_prompt_never_tells_the_model_to_count_candidates():
    prompt = _normalized()

    # The specific shortcut worth naming: picking candidates[0] when the domain
    # deliberately declined to resolve.
    assert "never take the first candidate as a shortcut" in prompt
    assert "do not re-derive that decision by counting candidates" in prompt
