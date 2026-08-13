"""T00B: probe Linear's GraphQL API and confirm the six facts T00L and T26
through T29 are built on.

Run this the way T00's probe is run, against the demo team only:

    $env:LINEAR_API_KEY = "<personal api key>"
    $env:LINEAR_TEAM_KEY = "TRE"
    python -B backend/scripts/linear_probe.py

The script reads both values from the environment directly, the way
`backend/scripts/api_probe.py` handles its own configuration. Wiring Linear
settings into `backend/app/config.py` belongs to T26.

This probe writes. It creates one throwaway issue in the demo team and archives
it before returning. It never calls `issueDelete`, and it must never be pointed
at the workspace or team holding the TAD project tickets. Nothing it writes
survives except the archived throwaway.

The key is read but never printed. Do not add it to any output, and do not
commit it.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

ENDPOINT = "https://api.linear.app/graphql"
TOTAL_FACTS = 6
PROBE_TITLE = "T00B probe throwaway, safe to archive"

class ProbeCheckError(AssertionError):
    """Raised when a probed API fact does not hold."""


def require(condition: object, message: str) -> None:
    # Deliberately not `assert`. Python strips assert statements under
    # PYTHONOPTIMIZE, which would let this probe print its success line while
    # verifying nothing. The T00R gate runs this file with PYTHONOPTIMIZE=1 for
    # exactly that reason.
    if not condition:
        raise ProbeCheckError(message)


@dataclass(frozen=True)
class ProbeResult:
    number: int
    name: str
    detail: str


class Linear:
    """The smallest client that can establish the six facts.

    Not a preview of `linear.py`. T26 owns that file and this class is
    deliberately not shaped as an abstraction to reuse.
    """

    def __init__(self, key: str) -> None:
        self._key = key
        self._client = httpx.Client(timeout=30.0)
        self.last_headers: httpx.Headers | None = None

    def post(
        self, query: str, variables: dict[str, Any] | None = None, *, header: str | None = None
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        # `header` exists so fact 1 can send a deliberately wrong Authorization
        # value. Everything else uses the confirmed raw form.
        headers["Authorization"] = self._key if header is None else header
        response = self._client.post(
            ENDPOINT,
            headers=headers,
            json={"query": query, "variables": variables or {}},
            )
        self.last_headers = response.headers
        return response

    def call(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.post(query, variables)
        payload = response.json()
        require(
            "errors" not in payload,
            f"GraphQL call failed: {payload.get('errors')}",
        )
        return payload["data"]

    def close(self) -> None:
        self._client.close()


VIEWER_QUERY = "{ viewer { id name } }"

TEAM_COLLECTION_QUERY = """
query($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes { id key name %s(first: 50) { nodes { %s } } }
  }
}
"""

ISSUE_FIELDS = (
    "id identifier title description priority dueDate updatedAt archivedAt "
    "state { id name } assignee { id name } project { id name } "
    "labels { nodes { id name } }"
)


def probe_authentication(linear: Linear, _team_key: str) -> ProbeResult:
    """Fact 1: the header format for a personal API key."""
    raw = linear.post(VIEWER_QUERY)
    require(
        raw.status_code == 200 and "errors" not in raw.json(),
        f"Authorization: <key> was rejected with HTTP {raw.status_code}: {raw.text[:200]}",
    )

    bearer = linear.post(VIEWER_QUERY, header=f"Bearer {linear._key}")
    require(
        bearer.status_code != 200 or "errors" in bearer.json(),
        "Authorization: Bearer <key> was accepted. Fact 1 recorded that the raw "
        "form is required, and a build that sends Bearer would now also work, "
        "which supersedes the recorded value in docs/DECISIONS.md.",
    )

    anonymous = linear.post(VIEWER_QUERY, header="")
    require(
        anonymous.status_code != 200 or "errors" in anonymous.json(),
        "an unauthenticated request succeeded",
    )

    return ProbeResult(
        1,
        "authentication header",
        "Authorization: <key> raw, no Bearer prefix. Bearer returns HTTP 400 and "
        "no header returns HTTP 401.",
    )


def probe_workspace_resolution(linear: Linear, team_key: str) -> ProbeResult:
    """Fact 2: enumerate the objects T26 builds tool argument enums from."""
    collections = {
        "states": "id name type position",
        "labels": "id name",
        "members": "id name email active",
        "projects": "id name state",
    }

    resolved: dict[str, list[dict[str, Any]]] = {}
    for field, selection in collections.items():
        data = linear.call(TEAM_COLLECTION_QUERY % (field, selection), {"key": team_key})
        nodes = data["teams"]["nodes"]
        require(nodes, f"team {team_key!r} did not resolve")
        resolved[field] = nodes[0][field]["nodes"]

    require(resolved["states"], "the demo team has no workflow states")
    require(resolved["members"], "the demo team has no members")
    # Labels and projects may legitimately be empty in a fresh workspace. An
    # empty projects list is not a Gate B failure, because enumeration
    # succeeded; it is a demo-readiness problem, because the `project` enum
    # T26 builds would have no members. Say which it is rather than passing
    # silently.
    require(
        resolved["projects"],
        "the demo team has zero projects, so the `project` enum T26 builds at "
        "startup would have no members. Create at least one project in the demo "
        "team. Enumeration itself works, so this is demo readiness rather than "
        "a Gate B failure.",
    )

    for field, nodes in resolved.items():
        names = [node["name"] for node in nodes]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        require(
            not duplicates,
            f"{field} names are not unique within the team: {duplicates}. Name to "
            "id resolution in T26 needs a tiebreak, which is an open question "
            "rather than a judgment call.",
        )

    return ProbeResult(
        2,
        "workspace object resolution",
        f"{len(resolved['states'])} states, {len(resolved['labels'])} labels, "
        f"{len(resolved['members'])} members, {len(resolved['projects'])} projects, "
        "each name unique within the team",
    )


def _team_context(linear: Linear, team_key: str) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for field, selection in (
        ("states", "id name type"),
        ("labels", "id name"),
        ("members", "id name"),
        ("projects", "id name"),
    ):
        data = linear.call(TEAM_COLLECTION_QUERY % (field, selection), {"key": team_key})
        node = data["teams"]["nodes"][0]
        context["id"] = node["id"]
        context[field] = node[field]["nodes"]
    return context


def _create_probe_issue(linear: Linear, team_id: str, client_id: str) -> dict[str, Any]:
    data = linear.call(
        "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { %s } } }"
        % ISSUE_FIELDS,
        {
            "input": {
                "id": client_id,
                "teamId": team_id,
                "title": PROBE_TITLE,
                "description": "Written by backend/scripts/linear_probe.py. Archived before the probe returns.",
            }
        },
    )
    require(data["issueCreate"]["success"], "issueCreate reported success=false")
    return data["issueCreate"]["issue"]


def _read_issue(linear: Linear, issue_id: str) -> dict[str, Any]:
    return linear.call(
        "query($id: String!) { issue(id: $id) { %s } }" % ISSUE_FIELDS, {"id": issue_id}
    )["issue"]


def _archive(linear: Linear, issue_id: str) -> bool:
    data = linear.call(
        "mutation($id: String!) { issueArchive(id: $id) { success } }", {"id": issue_id}
    )
    return bool(data["issueArchive"]["success"])


def _clearable_state(issue: dict[str, Any], field: str) -> object:
    """The observable value of a clearable field, normalized for comparison.

    Cleared is `()` for labels and `None` for project, so an emptiness test is
    the same test for both.
    """
    if field == "labelIds":
        return tuple(sorted(node["id"] for node in issue["labels"]["nodes"]))
    return issue["project"]["id"] if issue["project"] else None


def _bracketed_update(
    linear: Linear, issue_id: str, patch: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """Apply one update and read the issue back, with the sleeps fact 4 needs.

    The sleeps are load bearing rather than caution. Two mutations issued in
    quick succession can share an updatedAt because of timestamp resolution, and
    that is indistinguishable from a field that does not bump it. Every
    transition in a set then clear sequence gets the same bracket, not just the
    single-patch loop, or a stale reading cannot be trusted.
    """
    before = _read_issue(linear, issue_id)["updatedAt"]
    time.sleep(1.1)
    linear.call(
        "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }",
        {"id": issue_id, "input": patch},
    )
    time.sleep(1.0)
    after = _read_issue(linear, issue_id)
    return before, after["updatedAt"], after


def _probe_clear_case(
    linear: Linear,
    issue_id: str,
    field: str,
    set_patch: dict[str, Any],
    candidates: list[tuple[str, dict[str, Any]]],
) -> tuple[str, str, str]:
    """Whether clearing `field` bumps updatedAt, proved rather than inferred.

    Returns the outcome, the clear shape that actually worked, and a detail
    line. The shape is returned separately rather than only inside the detail
    text because which spelling Linear honours is itself a fact T26 and T28
    need, not an implementation note.

    Comparing updatedAt alone cannot separate a field that does not bump it from
    a mutation that never applied. For a set that rarely matters. For a clear it
    decides the answer, so the value is read back at every step and the outcome
    is one of four rather than two. Recording a no-op as "clearing does not move
    updatedAt" would put a false claim into the fact 4 table that T28's
    reconciler is built on.

    Several shapes can express a clear, because GraphQL distinguishes an
    explicit null from an absent key, and which one Linear honours is behaviour
    rather than schema. Introspection would only say which values are
    schema-valid, so the read-back is the authoritative test. The candidates are
    tried in one pass so shape discovery does not cost another live run against
    the demo workspace. `Linear.call` already raises on a GraphQL errors array,
    so a rejected shape arrives here as ProbeCheckError and moves to the next
    candidate rather than being mistaken for a no-op.
    """
    # The precondition is state, not a timestamp: the field may already hold
    # this value from the patch loop above, in which case the set is a no-op and
    # only the read-back matters.
    _, _, after_set = _bracketed_update(linear, issue_id, set_patch)
    if not _clearable_state(after_set, field):
        return (
            "PRECONDITION_SET_FAILED",
            "",
            f"{field} could not be established in the starting state, so this is a "
            "fact 3 shape problem and not a clear-case result",
        )

    attempted: list[str] = []
    for shape, clear_patch in candidates:
        attempted.append(shape)
        try:
            before, after, after_clear = _bracketed_update(linear, issue_id, clear_patch)
        except ProbeCheckError:
            continue
        if _clearable_state(after_clear, field):
            continue
        if after == before:
            return (
                "APPLIED_BUT_STALE",
                shape,
                f"{shape} cleared {field} and updatedAt did not move, so T28 cannot "
                "detect this external mutation from the fact 4 query",
            )
        return (
            "APPLIED_AND_BUMPED",
            shape,
            f"{shape} cleared {field} and updatedAt moved",
        )

    return (
        "CLEAR_SHAPE_EXHAUSTED",
        "",
        f"no candidate shape cleared {field}, tried {attempted}. This is a probe or "
        "API shape ambiguity, not evidence that Linear cannot clear the field",
    )


def probe_mutation_shapes(linear: Linear, team_key: str) -> ProbeResult:
    """Fact 3: create, update, archive, and the unarchive that undo depends on."""
    context = _team_context(linear, team_key)
    client_id = str(uuid.uuid4())
    issue = _create_probe_issue(linear, context["id"], client_id)
    issue_id = issue["id"]

    try:
        require(
            issue_id == client_id,
            f"issueCreate ignored the client-supplied id: asked {client_id}, got {issue_id}",
        )

        # Every field this build sets, applied through issueUpdate.
        patch: dict[str, Any] = {
            "title": PROBE_TITLE + " updated",
            "description": "updated by the probe",
            "priority": 2,
            "dueDate": "2026-08-20",
            "stateId": context["states"][0]["id"],
            "assigneeId": context["members"][0]["id"],
        }
        if context["labels"]:
            patch["labelIds"] = [context["labels"][0]["id"]]
        if context["projects"]:
            patch["projectId"] = context["projects"][0]["id"]

        data = linear.call(
            "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { %s } } }"
            % ISSUE_FIELDS,
            {"id": issue_id, "input": patch},
        )
        require(data["issueUpdate"]["success"], "issueUpdate reported success=false")
        updated = data["issueUpdate"]["issue"]
        require(updated["priority"] == 2, f"priority did not apply: {updated['priority']}")
        require(updated["dueDate"] == "2026-08-20", f"dueDate did not apply: {updated['dueDate']}")
        require(
            updated["state"]["id"] == context["states"][0]["id"],
            "stateId did not apply",
        )

        # The 3:00 demo beat is a destructive action followed by undo, so the
        # round trip is what matters, not the archive alone.
        require(_archive(linear, issue_id), "issueArchive reported success=false")
        time.sleep(1.0)
        archived = _read_issue(linear, issue_id)
        require(archived["archivedAt"] is not None, "issueArchive left archivedAt null")

        data = linear.call(
            "mutation($id: String!) { issueUnarchive(id: $id) { success } }", {"id": issue_id}
        )
        require(data["issueUnarchive"]["success"], "issueUnarchive reported success=false")
        time.sleep(1.0)
        restored = _read_issue(linear, issue_id)
        require(
            restored["archivedAt"] is None,
            "issueUnarchive did not clear archivedAt, so the undo beat leaves the "
            "Linear issue archived while the local board shows the task restored",
        )
        require(
            restored["title"] == patch["title"] and restored["priority"] == 2,
            "unarchive did not preserve the issue's fields",
        )
    finally:
        _archive(linear, issue_id)

    return ProbeResult(
        3,
        "mutation shapes",
        "issueCreate(input: IssueCreateInput!), issueUpdate(id, input: IssueUpdateInput!), "
        "issueArchive(id, trash), issueUnarchive(id). Archive and unarchive round-trip "
        "with fields intact.",
    )


def probe_change_detection(linear: Linear, team_key: str) -> ProbeResult:
    """Fact 4: the query T28's reconciler polls on, and what it can miss."""
    context = _team_context(linear, team_key)
    client_id = str(uuid.uuid4())
    issue = _create_probe_issue(linear, context["id"], client_id)
    issue_id = issue["id"]

    try:
        # Every field this build cares about must bump updatedAt, or the
        # reconciler cannot see a human's edit to it.
        patches: list[tuple[str, dict[str, Any]]] = [
            ("title", {"title": PROBE_TITLE + " t"}),
            ("description", {"description": "d"}),
            ("priority", {"priority": 3}),
            ("dueDate", {"dueDate": "2026-08-21"}),
            ("stateId", {"stateId": context["states"][0]["id"]}),
            ("assigneeId", {"assigneeId": context["members"][0]["id"]}),
        ]
        if context["labels"]:
            patches.append(("labelIds", {"labelIds": [context["labels"][0]["id"]]}))
        if context["projects"]:
            patches.append(("projectId", {"projectId": context["projects"][0]["id"]}))

        stale: list[str] = []
        for name, patch in patches:
            before, after, _ = _bracketed_update(linear, issue_id, patch)
            if after == before:
                stale.append(name)

        require(
            not stale,
            f"updatedAt did not move for {stale}. The reconciler cannot detect a "
            "human editing those fields, so the divergence guarantee has a hole "
            "in it. This is a Gate B finding.",
        )

        # Clearing is not implied by setting. `labelIds` replaces rather than
        # appends and `project` is nullable, so an empty label set and a
        # detached project are both representable in the surface T26 exposes and
        # reachable through T28's reconciler. Whether clearing bumps updatedAt is
        # therefore a fact this build depends on, and the set cases above say
        # nothing about it.
        # A skipped clear case must not read as a passing one. Without a label
        # and a project to detach there is nothing to measure, and staying
        # silent about that is the interpretive gap this fact is meant to close.
        require(
            context["labels"] and context["projects"],
            "the demo team needs at least one label and one project before the "
            "clear cases can be measured at all",
        )

        cleared: list[tuple[str, str, str, str]] = []
        for field, set_patch, candidates in (
            (
                "labelIds",
                {"labelIds": [context["labels"][0]["id"]]},
                [("[]", {"labelIds": []})],
            ),
            (
                "projectId",
                {"projectId": context["projects"][0]["id"]},
                [("null", {"projectId": None})],
            ),
        ):
            outcome, shape, detail = _probe_clear_case(
                linear, issue_id, field, set_patch, candidates
            )
            cleared.append((field, outcome, shape, detail))

        for field, outcome, _shape, detail in cleared:
            require(
                outcome == "APPLIED_AND_BUMPED",
                f"{field} clear: {outcome}. {detail}. The probe stops rather than "
                "record an unmeasured clear case as confirmed, because the fact 4 "
                "table is what T28's reconciler is built on.",
            )

        # Emitted, not inferred. A future reader must not have to reconstruct
        # control flow to learn whether these ran or which spelling worked.
        clear_evidence = ", ".join(
            f"{field} clear via {shape} -> {outcome}" for field, outcome, shape, _ in cleared
        )

        # The filter and cursor T28 polls with.
        data = linear.call(
            """
            query($key: String!, $since: DateTimeOrDuration!) {
              issues(
                filter: { team: { key: { eq: $key } }, updatedAt: { gt: $since } }
                first: 1
                orderBy: updatedAt
              ) {
                pageInfo { hasNextPage endCursor }
                nodes { id updatedAt }
              }
            }
            """,
            {"key": team_key, "since": "2020-01-01T00:00:00.000Z"},
        )
        page = data["issues"]["pageInfo"]
        require("hasNextPage" in page and "endCursor" in page, "pagination shape changed")

        # D-27 requires archived issues to be excluded, or a deleted task
        # resurrects through the reconciler's import path on the next poll.
        require(_archive(linear, issue_id), "archive during fact 4 failed")
        time.sleep(1.5)

        default_ids = {
            node["id"]
            for node in linear.call(
                'query($key: String!) { issues(filter: { team: { key: { eq: $key } } }, first: 100) { nodes { id } } }',
                {"key": team_key},
            )["issues"]["nodes"]
        }
        included_ids = {
            node["id"]
            for node in linear.call(
                'query($key: String!) { issues(filter: { team: { key: { eq: $key } } }, includeArchived: true, first: 100) { nodes { id } } }',
                {"key": team_key},
            )["issues"]["nodes"]
        }
        require(
            issue_id not in default_ids,
            "an archived issue is still returned by the default query, so a "
            "deleted task would resurrect through the import path",
        )
        require(
            issue_id in included_ids,
            "includeArchived: true did not return the archived issue",
        )
    finally:
        _archive(linear, issue_id)

    return ProbeResult(
        4,
        "change detection",
        "filter { team: { key: { eq } }, updatedAt: { gt } } with orderBy: updatedAt and "
        "pageInfo { hasNextPage endCursor }. updatedAt moves for every mapped field set "
        f"one at a time. Clear cases, each proved by reading the field back before the "
        f"timestamp is judged: {clear_evidence}. Archived issues are excluded by default.",
    )


def probe_key_scope_and_limits(linear: Linear, team_key: str) -> ProbeResult:
    """Fact 5: what the key reaches, and whether the limits clear demo usage."""
    data = linear.call("{ viewer { id name } organization { id name urlKey } }")
    require(data["viewer"]["id"], "the key did not resolve a viewer")
    require(data["organization"]["id"], "the key did not resolve an organization")

    # No team parameter is supplied anywhere in that query. A team-scoped
    # credential could not answer it, so the key is user-scoped and the demo
    # team restriction is a policy check in our code rather than an API
    # guarantee. That distinction goes in the README.
    teams = linear.call("{ teams(first: 50) { nodes { id key } } }")["teams"]["nodes"]
    keys = [team["key"] for team in teams]
    require(team_key in keys, f"the demo team {team_key!r} is not reachable; saw {keys}")

    headers = linear.last_headers or {}
    requests_limit = headers.get("x-ratelimit-requests-limit")
    complexity_limit = headers.get("x-ratelimit-complexity-limit")
    require(
        requests_limit is not None,
        "no x-ratelimit-requests-limit header, so the limit cannot be observed",
    )

    return ProbeResult(
        5,
        "key scope and limits",
        f"user-scoped: resolves viewer and organization with no team parameter and "
        f"sees {len(teams)} team(s). Observed limits {requests_limit} requests and "
        f"{complexity_limit} complexity per window, comfortably above the roughly 25 "
        "calls a demo reset costs.",
    )


def probe_delivery_deduplication(linear: Linear, team_key: str) -> ProbeResult:
    """Fact 6: is there a client key with documented replay semantics?

    Accepting a client-controlled identifier is API shape. What T27 would need
    is behaviour: that replaying the same mutation with the same key cannot
    apply the operation twice. Only that earns a second layer on top of the
    local UNIQUE(event_id) constraint.
    """
    context = _team_context(linear, team_key)

    # No mutation argument anywhere in the schema advertises replay semantics.
    mutation_fields = linear.call(
        '{ __type(name: "Mutation") { fields { name args { name } } } }'
    )["__type"]["fields"]
    argument_names = {
        argument["name"].lower()
        for field in mutation_fields
        for argument in field["args"]
    }
    advertised = sorted(
        name
        for name in argument_names
        if any(token in name for token in ("idempot", "clientmutation", "dedup"))
    )
    require(
        not advertised,
        f"a mutation argument now advertises deduplication: {advertised}. Fact 6 "
        "recorded that none exists, and T27 may now be able to take a second "
        "layer. Re-read the documentation before changing the answer to yes.",
    )

    # The only client-controlled identifier is IssueCreateInput.id. Replaying a
    # create with the same id is rejected as a conflict rather than returning
    # the original issue, so it is a uniqueness constraint and not a replay
    # guarantee.
    client_id = str(uuid.uuid4())
    issue = _create_probe_issue(linear, context["id"], client_id)
    issue_id = issue["id"]

    try:
        replay_title = PROBE_TITLE + " replay"
        replay = linear.post(
            "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id } } }",
            {
                "input": {
                    "id": client_id,
                    "teamId": context["id"],
                    "title": replay_title,
                }
            },
        ).json()
        require(
            "errors" in replay,
            "replaying issueCreate with the same client-supplied id succeeded. If "
            "it now returns the original issue rather than conflicting, Linear may "
            "have gained replay semantics, which would supersede fact 6.",
        )

        # Both checks below are keyed on an id or an exact title rather than on a
        # title substring. A substring scan would fold every archived issue left
        # by an earlier probe run into this assertion, and with a page limit and
        # no ordering it would eventually fail by reporting a replay that never
        # happened.
        original = linear.call(
            "query($id: String!) { issue(id: $id) { id title } }", {"id": issue_id}
        )["issue"]
        require(
            original is not None and original["id"] == issue_id,
            "the original issue no longer resolves under its client-supplied id",
        )
        require(
            original["title"] == PROBE_TITLE,
            f"the replay overwrote the original issue's title with {original['title']!r}. "
            "That is upsert behaviour rather than a uniqueness constraint, and it "
            "would change the answer to fact 6.",
        )

        duplicates = linear.call(
            'query($key: String!, $title: String!) { issues(filter: { team: { key: { eq: $key } }, title: { eq: $title } }, includeArchived: true, first: 1) { nodes { id } } }',
            {"key": team_key, "title": replay_title},
        )["issues"]["nodes"]
        require(
            not duplicates,
            "the replay created a second issue under a different id",
        )
    finally:
        _archive(linear, issue_id)

    return ProbeResult(
        6,
        "delivery deduplication",
        "No. No mutation argument advertises replay or idempotency semantics. "
        "IssueCreateInput.id is client-supplied but a replay is rejected with "
        "'Entity Issue with id ... already exists', which is a uniqueness "
        "constraint. Local UNIQUE(event_id) is the only deduplication layer.",
    )


CHECKS: list[tuple[int, str, Callable[[Linear, str], ProbeResult]]] = [
    (1, "authentication header", probe_authentication),
    (2, "workspace object resolution", probe_workspace_resolution),
    (3, "mutation shapes", probe_mutation_shapes),
    (4, "change detection", probe_change_detection),
    (5, "key scope and limits", probe_key_scope_and_limits),
    (6, "delivery deduplication", probe_delivery_deduplication),
]


def main() -> int:
    print("Trellis T00B Linear API probe")
    print(f"Python {sys.version.split()[0]}")
    print(f"httpx {httpx.__version__}")

    key = os.environ.get("LINEAR_API_KEY")
    team_key = os.environ.get("LINEAR_TEAM_KEY")
    if not key or not team_key:
        print()
        print(
            "LINEAR_API_KEY and LINEAR_TEAM_KEY must both be set. The probe reads "
            "them from the environment directly; see the module docstring."
        )
        return 2

    print(f"team {team_key}")
    print()

    linear = Linear(key)
    confirmed: list[ProbeResult] = []
    try:
        for number, name, check in CHECKS:
            try:
                confirmed.append(check(linear, team_key))
            except Exception as exc:
                for result in confirmed:
                    print(f"PASS {result.number}/{TOTAL_FACTS} {result.name}: {result.detail}")
                print(f"FAIL {number}/{TOTAL_FACTS} {name}: {type(exc).__name__}: {exc}")
                print()
                traceback.print_exc(file=sys.stdout)
                print()
                print(
                    f"Fact {number} of {TOTAL_FACTS} no longer holds for Linear's API. "
                    "Update the matching row in docs/DECISIONS.md and every task that "
                    "consumes it before changing this probe to agree with the new "
                    "behaviour. Facts 2, 3, and 4 carry the Gate B criteria."
                )
                return 1
    finally:
        linear.close()

    for result in confirmed:
        print(f"PASS {result.number}/{TOTAL_FACTS} {result.name}: {result.detail}")
    print(f"ALL {TOTAL_FACTS} LINEAR API FACTS CONFIRMED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
