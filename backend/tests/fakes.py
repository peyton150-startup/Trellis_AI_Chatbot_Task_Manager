"""FakeTracker: an in-process stand-in for the surface `linear.py` exposes.

**This is a test double, not a provider abstraction.** There is one external
system in this build and it is Linear. `linear.py` is a client, not an interface
with one implementation, and nothing here is designed so that Jira could be
dropped in. A future reader should not infer otherwise from the existence of
this file.

It exists so the Linear-facing tasks can be tested offline. The deterministic
invariant suite is CI-gating at 100 percent and must never require a network,
per D-09, and T26 through T29 exercise resolution, delivery, and reconciliation
without belonging to that suite.

The behaviour here is copied from what Linear actually does, measured at T00B
and recorded in the T00B fact table in `docs/DECISIONS.md`. Three of those
behaviours are counterintuitive and are the reason this fake was written at the
same time as the contract fixture rather than guessed at later:

1. Archiving does **not** move `updated_at`. An archived issue leaves the
   default listing instead of appearing as a modification, so a reconciler
   polling on `updated_at` cannot see an archival at all.
2. Archived issues are excluded from listings by default and appear only when
   they are asked for explicitly. Without that, a deleted task resurrects
   through the reconciler's import path.
3. Creating with an id that already exists is a conflict, not a replay. It
   raises rather than returning the existing issue, so a retry after an
   ambiguous create must treat the conflict as evidence the create landed.

Times are supplied by the caller rather than read from the clock, so tests stay
deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


class TrackerConflict(Exception):
    """Raised when a create reuses an id that already exists.

    Mirrors Linear's "Entity Issue with id <uuid> already exists", which is a
    uniqueness constraint rather than replay semantics. The distinction is the
    whole of T00B fact 6: a client-supplied identifier without a deduplication
    guarantee does not earn a second layer on top of the local
    UNIQUE(event_id) constraint.
    """


class TrackerNotFound(Exception):
    """Raised when an id does not resolve to an issue."""


@dataclass(frozen=True)
class FakeIssue:
    id: str
    team_key: str
    title: str
    updated_at: str
    description: str = ""
    state: str | None = None
    priority: int | None = None
    due_date: str | None = None
    assignee: str | None = None
    project: str | None = None
    labels: tuple[str, ...] = ()
    archived_at: str | None = None


# The fields a projection can carry outward. `blocked_by` is deliberately absent:
# T06 emits an `updated` event when a delete clears another task's `blocked_by`,
# and that field has no Linear representation, so D-28 requires the projector to
# complete such a row without a remote call.
MAPPED_FIELDS = frozenset(
    {"title", "description", "state", "priority", "due_date", "assignee", "project", "labels"}
)


@dataclass
class FakeTracker:
    """The surface T26's `linear.py` exposes, in process.

    `names` maps a collection to the names that exist in it, mirroring the
    workspace objects T26 builds tool argument enums from. Names are unique
    within a team, confirmed at T00B, so resolution needs no tiebreak.
    """

    team_key: str = "TRE"
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    issues: dict[str, FakeIssue] = field(default_factory=dict)

    # ------------------------------------------------------------------ resolve
    def resolve(self, collection: str, name: str) -> str:
        """Resolve a human-facing name to the id the wire carries.

        BUILD_SPEC section 10 requires enums rather than free strings, and
        Linear's states, labels, projects, and members are workspace-defined
        objects with UUIDs. The model works in names; the adapter resolves.
        """
        known = self.names.get(collection)
        if known is None:
            raise TrackerNotFound(f"unknown collection {collection!r}")
        if name not in known:
            raise TrackerNotFound(f"{collection} has no member named {name!r}")
        return known[name]

    def members(self, collection: str) -> list[str]:
        return sorted(self.names.get(collection, {}))

    # ------------------------------------------------------------------- create
    def create_issue(self, issue_id: str, title: str, now: str, **fields: Any) -> FakeIssue:
        if issue_id in self.issues:
            raise TrackerConflict(f"Entity Issue with id {issue_id} already exists")
        unknown = set(fields) - MAPPED_FIELDS
        if unknown:
            raise ValueError(f"not mapped to Linear: {sorted(unknown)}")
        if "labels" in fields:
            fields["labels"] = tuple(fields["labels"])
        issue = FakeIssue(
            id=issue_id, team_key=self.team_key, title=title, updated_at=now, **fields
        )
        self.issues[issue_id] = issue
        return issue

    # ------------------------------------------------------------------- update
    def update_issue(self, issue_id: str, now: str, **fields: Any) -> FakeIssue:
        issue = self._require(issue_id)
        unknown = set(fields) - MAPPED_FIELDS
        if unknown:
            raise ValueError(f"not mapped to Linear: {sorted(unknown)}")
        if "labels" in fields:
            # Replacement rather than append, which is what an approval diff can
            # preview cleanly.
            fields["labels"] = tuple(fields["labels"])
        if not fields:
            # No mapped field changed, so there is nothing to send. D-28 has the
            # projector complete such a row without a remote call rather than
            # issuing an empty mutation and counting it as work.
            return issue
        updated = replace(issue, updated_at=now, **fields)
        self.issues[issue_id] = updated
        return updated

    # ---------------------------------------------------------------- lifecycle
    def archive_issue(self, issue_id: str, now: str) -> FakeIssue:
        """Archive, leaving `updated_at` alone.

        Measured at T00B: an archive does not move `updated_at`. Keeping that
        here is the point of the fake. A test written against a fake that did
        move it would prove a reconciler behaviour that does not hold against
        the real API.
        """
        issue = self._require(issue_id)
        archived = replace(issue, archived_at=now)
        self.issues[issue_id] = archived
        return archived

    def unarchive_issue(self, issue_id: str, now: str) -> FakeIssue:
        """Restore an archived issue with its fields intact.

        The 3:00 demo beat is a destructive action followed by undo, and D-25
        maps `restored` to unarchive rather than update precisely because an
        update would leave the issue archived while the local board showed the
        task back.
        """
        issue = self._require(issue_id)
        restored = replace(issue, archived_at=None)
        self.issues[issue_id] = restored
        return restored

    # ------------------------------------------------------------------- listing
    def issues_changed_since(
        self, since: str, *, include_archived: bool = False
    ) -> list[FakeIssue]:
        """The reconciler's poll.

        Archived issues are excluded unless asked for, per D-27. Without that a
        deleted task resurrects: the local row is gone, the issue is archived,
        and the import path sees an issue with no local row and recreates it.
        """
        selected = [
            issue
            for issue in self.issues.values()
            if issue.updated_at > since and (include_archived or issue.archived_at is None)
        ]
        return sorted(selected, key=lambda issue: (issue.updated_at, issue.id))

    def get_issue(self, issue_id: str) -> FakeIssue | None:
        return self.issues.get(issue_id)

    def _require(self, issue_id: str) -> FakeIssue:
        issue = self.issues.get(issue_id)
        if issue is None:
            raise TrackerNotFound(f"no issue with id {issue_id}")
        return issue
