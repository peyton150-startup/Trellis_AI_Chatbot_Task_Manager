"""Drift test: live Linear introspection against the frozen contract fixture.

Marked `contract` because of what it is, a provider contract test, and `network`
because of what it requires. Only `network` decides what CI collects, so this
file is excluded from `pytest -m "not network"` while `pytest -m contract` runs
it deliberately. See D-32 and the test marker contract in BUILD_SPEC section 11.

The point is that a change to Linear's schema surfaces as a named failing test
rather than as a confusing runtime error during rehearsal on day six. It is not
an SDK generation project, and the fixture holds only the subset this build
depends on.

When this fails, the fix is almost never to regenerate the fixture. A change
here means an assumption in T00L, T26, T27, T28, or T29 moved, and those are
what need updating first. Regenerating to restore green throws away the warning.

    pytest -m contract

Requires LINEAR_API_KEY. Reads no team, mutates nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.contract, pytest.mark.network]

ENDPOINT = "https://api.linear.app/graphql"
FIXTURE = Path(__file__).parent / "fixtures" / "linear_contract.json"

TYPE_QUERY = """
query($name: String!) {
  __type(name: $name) {
    name kind
    fields(includeDeprecated: true) {
      name
      isDeprecated
      type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
    }
    inputFields {
      name
      type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
    }
  }
}
"""

MUTATION_QUERY = """
{
  __type(name: "Mutation") {
    fields {
      name
      args { name type { kind name ofType { kind name ofType { kind name } } } }
      type { kind name ofType { kind name ofType { kind name } } }
    }
  }
}
"""


def render_type(node: dict | None) -> str:
    """Render an introspected type reference the way the fixture stores it."""
    if node is None:
        return "?"
    if node["kind"] == "NON_NULL":
        return render_type(node.get("ofType")) + "!"
    if node["kind"] == "LIST":
        return "[" + render_type(node.get("ofType")) + "]"
    return node.get("name") or "?"


@pytest.fixture(scope="module")
def contract() -> dict:
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        pytest.skip(
            "LINEAR_API_KEY is not set, so the live half of the contract test "
            "cannot run. This test is excluded from CI by its network marker and "
            "is expected to be run deliberately with the key present."
        )
    # T00B fact 1: the key goes in Authorization raw, with no Bearer prefix.
    with httpx.Client(
        base_url=ENDPOINT,
        headers={"Authorization": key, "Content-Type": "application/json"},
        timeout=30.0,
    ) as session:
        yield session


def call(client: httpx.Client, query: str, variables: dict | None = None) -> dict:
    response = client.post("", json={"query": query, "variables": variables or {}})
    payload = response.json()
    assert "errors" not in payload, f"introspection failed: {payload.get('errors')}"
    return payload["data"]


def test_fixture_parses_and_is_the_expected_shape(contract: dict) -> None:
    """Offline. The only part of this file CI could run, if it collected it."""
    assert contract["endpoint"] == ENDPOINT
    assert contract["types"], "the fixture records no types"
    assert contract["mutations"], "the fixture records no mutations"
    for name, entry in contract["types"].items():
        assert entry["fields"], f"{name} has no recorded fields"
        assert entry["kind"] in {"OBJECT", "INPUT_OBJECT"}, f"{name} has kind {entry['kind']}"


@pytest.mark.parametrize(
    "type_name",
    [
        "Issue",
        "IssueCreateInput",
        "IssueUpdateInput",
        "IssuePayload",
        "IssueArchivePayload",
        "WorkflowState",
        "IssueLabel",
        "Project",
        "Team",
        "User",
    ],
)
def test_type_matches_the_frozen_contract(
    client: httpx.Client, contract: dict, type_name: str
) -> None:
    expected = contract["types"][type_name]
    live = call(client, TYPE_QUERY, {"name": type_name})["__type"]
    assert live is not None, f"{type_name} no longer exists in Linear's schema"
    assert live["kind"] == expected["kind"], (
        f"{type_name} changed kind from {expected['kind']} to {live['kind']}"
    )

    members = live.get("fields") or live.get("inputFields") or []
    # includeDeprecated matters: Linear already serves at least one depended-on
    # field that introspection hides by default, and without this the test would
    # report it as removed.
    observed = {member["name"]: render_type(member["type"]) for member in members}

    missing = sorted(name for name in expected["fields"] if name not in observed)
    assert not missing, (
        f"{type_name} no longer exposes {missing}, which this build reads or writes"
    )

    changed = {
        name: (recorded, observed[name])
        for name, recorded in expected["fields"].items()
        if observed[name] != recorded
    }
    assert not changed, (
        f"{type_name} changed types for {changed}. Update the task that consumes "
        "the field before regenerating the fixture."
    )

    newly_deprecated = sorted(
        member["name"]
        for member in members
        if member.get("isDeprecated")
        and member["name"] in expected["fields"]
        and member["name"] not in expected.get("deprecated", [])
    )
    assert not newly_deprecated, (
        f"{type_name} newly deprecated {newly_deprecated}, which this build depends "
        "on. A deprecation is a removal with a delay on it."
    )


def test_mutations_match_the_frozen_contract(client: httpx.Client, contract: dict) -> None:
    live = call(client, MUTATION_QUERY)["__type"]["fields"]
    by_name = {field["name"]: field for field in live}

    for name, expected in contract["mutations"].items():
        assert name in by_name, f"mutation {name} no longer exists"
        mutation = by_name[name]

        observed_args = {arg["name"]: render_type(arg["type"]) for arg in mutation["args"]}
        missing = sorted(arg for arg in expected["args"] if arg not in observed_args)
        assert not missing, f"{name} no longer accepts {missing}"

        changed = {
            arg: (recorded, observed_args[arg])
            for arg, recorded in expected["args"].items()
            if observed_args[arg] != recorded
        }
        assert not changed, f"{name} changed argument types for {changed}"

        returns = render_type(mutation["type"])
        assert returns == expected["returns"], (
            f"{name} now returns {returns}, recorded as {expected['returns']}"
        )


def test_no_mutation_advertises_replay_semantics(client: httpx.Client) -> None:
    """T00B fact 6 recorded a No, and this is what would change the answer.

    Accepting a client-controlled identifier is API shape. What T27 would rely
    on is behaviour: that replaying the same mutation with the same key cannot
    apply the operation twice. If an argument advertising deduplication appears,
    read the documentation before assuming the guarantee exists.
    """
    live = call(client, MUTATION_QUERY)["__type"]["fields"]
    advertised = sorted(
        {
            arg["name"]
            for mutation in live
            for arg in mutation["args"]
            if any(token in arg["name"].lower() for token in ("idempot", "clientmutation", "dedup"))
        }
    )
    assert not advertised, (
        f"a mutation argument now advertises deduplication: {advertised}. Fact 6 in "
        "docs/DECISIONS.md recorded that none existed, and D-25 allows a second "
        "layer only for documented replay semantics."
    )
