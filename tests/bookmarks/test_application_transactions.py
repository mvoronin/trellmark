"""Deterministic lifecycle failures; PostgreSQL semantics live in adapter tests."""

import threading

import pytest
from anyio import CapacityLimiter, run

from tests.bookmarks.helpers import make_icon_service
from tests.helpers import RecordingTitleFetcher
from trellmark.bookmarks import domain, persistence
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.platform.runtime import AnyIOWorkRunner

RECORD = domain.URLRecord(
    1, "https://url.example", "Title", "2026-09-05T00:00:00Z", False, 1
)
GROUP = domain.GroupRecord(2, "Group", None, 1, 1, False, (), ())
OPERATIONS = (
    ("edit_url", domain.EditURL(1, 1, title="Edited"), domain.URLUpdated(RECORD)),
    ("move_url", domain.MoveURL(1, 2), domain.URLMoved(RECORD, 2, 1)),
    ("remove_url", domain.RemoveURL(1, 1), domain.URLRemoved(RECORD, 1)),
    (
        "set_important",
        domain.SetImportant(1, True),
        domain.SetImportantSucceeded(RECORD),
    ),
    ("create_group", domain.CreateGroup("Group"), domain.GroupCreated(GROUP)),
    ("update_group", domain.UpdateGroup(2, name="Group"), domain.GroupUpdated(GROUP)),
    (
        "delete_group",
        domain.DeleteGroup(2, "delete"),
        domain.GroupDeleted(2, "delete", 0, 0),
    ),
    (
        "reorder_groups",
        domain.ReorderGroups(None, (1, 2)),
        domain.GroupsReordered(None, (1, 2)),
    ),
)
REJECTIONS = (
    (0, domain.URLNotFound(1)),
    (0, domain.URLConflict(RECORD.url)),
    (0, domain.URLVersionConflict(1, 1)),
    (0, domain.EmptyURLEdit(1)),
    (1, domain.URLNotFound(1)),
    (1, domain.GroupNotFound(2)),
    (1, domain.URLSourceRequired(1, (1, 3))),
    (1, domain.URLMembershipNotFound(1, 3)),
    (2, domain.URLNotFound(1)),
    (2, domain.URLMembershipNotFound(1, 3)),
    (3, domain.URLNotFound(1)),
    (4, domain.GroupNameConflict("Group")),
    (4, domain.ParentNotFound(3)),
    (4, domain.ParentIsSelfOrDescendant(3)),
    (4, domain.GroupDepthExceeded()),
    (5, domain.GroupNotFound(2)),
    (5, domain.DefaultGroupProtected(2, "edit")),
    (5, domain.GroupNameConflict("Group")),
    (5, domain.ParentNotFound(3)),
    (5, domain.ParentIsSelfOrDescendant(3)),
    (5, domain.GroupDepthExceeded()),
    (6, domain.GroupNotFound(2)),
    (6, domain.DefaultGroupProtected(2, "delete")),
    (6, domain.GroupHasChildren(2)),
    (7, domain.InvalidGroupOrder(None, (1, 2))),
)


class OperationFailure(RuntimeError):
    pass


class Lifecycle:
    def __init__(self, outcome, failure_at=None, cleanup_failure=None):
        self.outcome = outcome
        self.failure_at = failure_at
        self.failure = OperationFailure("synthetic operation failure")
        self.cleanup_failure = cleanup_failure
        self.events = []
        self.threads = []
        self.is_active = True

    def record(self, stage):
        self.events.append(stage)
        self.threads.append(threading.get_ident())
        if stage == self.failure_at:
            raise self.failure

    def connect(self):
        self.record("connect")
        return self

    def begin(self):
        self.record("begin")
        return self

    def scalar(self, statement):
        self.record("gate")
        assert "pg_try_advisory_xact_lock" in str(statement)
        assert set(statement.compile().params.values()) == {7502, 0}
        return self.failure_at != "gate-conflict"

    def commit(self):
        self.record("commit")
        self.is_active = False

    def rollback(self):
        self.record("rollback")
        self.is_active = False
        if self.cleanup_failure == "rollback":
            raise OperationFailure("synthetic rollback failure")

    def close(self):
        self.record("close")
        if self.cleanup_failure == "close":
            raise OperationFailure("synthetic close failure")


class Repository:
    def __init__(self, connection):
        connection.record("bind-bookmarks")
        self.connection = connection

    def execute(self, _command):
        assert self.connection.is_active
        self.connection.record("dml")
        self.connection.record("repository")
        return self.connection.outcome

    edit_url = move_url = remove_url = set_important = execute


class GroupRepository(Repository):
    def __init__(self, connection):
        connection.record("bind-groups")
        self.connection = connection

    create_group = update_group = delete_group = reorder_groups = Repository.execute


def invoke(monkeypatch, lifecycle, operation):
    method, command, _ = operation
    monkeypatch.setattr(persistence, "PostgresBookmarkRepository", Repository)
    monkeypatch.setattr(persistence, "PostgresGroupRepository", GroupRepository)
    event_loop_thread = threading.get_ident()

    async def exercise():
        units = []

        def factory():
            assert threading.get_ident() != event_loop_thread
            unit = persistence.PostgresLogicalBookmarkUnitOfWork(lambda: lifecycle)
            units.append(unit)
            return unit

        class CompleteClosureRunner:
            async def run(self, work):
                def complete():
                    try:
                        return work()
                    finally:
                        # Construction, binding, work and disposal all occur
                        # before the worker returns its immutable outcome.
                        assert threading.get_ident() != event_loop_thread
                        assert len(units) == 1
                        unit = units.pop()
                        assert unit._connection is None
                        assert unit._transaction is None
                        for name in ("bookmarks", "groups"):
                            if hasattr(unit, name):
                                assert getattr(unit, name).connection is lifecycle
                        assert lifecycle.events[-1] == "close"

                return await AnyIOWorkRunner(CapacityLimiter(4)).run(complete)

        service = BookmarksApplicationService(
            CompleteClosureRunner(),
            factory,
            None,
            None,
            RecordingTitleFetcher(),
            make_icon_service(),
        )
        return await getattr(service, method)(command)

    try:
        return run(exercise)
    finally:
        assert len(set(lifecycle.threads)) == 1
        assert lifecycle.threads[0] != threading.get_ident()


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda item: item[0])
def test_bookmark_success_commits_and_closes_in_one_worker(monkeypatch, operation):
    lifecycle = Lifecycle(operation[2])
    assert invoke(monkeypatch, lifecycle, operation) is operation[2]
    assert lifecycle.events == [
        "connect",
        "begin",
        "gate",
        "bind-bookmarks",
        "bind-groups",
        "dml",
        "repository",
        "commit",
        "close",
    ]


@pytest.mark.parametrize("index,outcome", REJECTIONS)
def test_bookmark_expected_rejection_rolls_back_and_closes(monkeypatch, index, outcome):
    lifecycle = Lifecycle(outcome)
    assert invoke(monkeypatch, lifecycle, OPERATIONS[index]) is outcome
    assert lifecycle.events == [
        "connect",
        "begin",
        "gate",
        "bind-bookmarks",
        "bind-groups",
        "dml",
        "repository",
        "rollback",
        "close",
    ]


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda item: item[0])
@pytest.mark.parametrize(
    "failure_at", ["repository", "commit", "gate", "bind-bookmarks", "bind-groups"]
)
def test_bookmark_failure_preserves_identity_rolls_back_and_closes(
    monkeypatch, operation, failure_at
):
    lifecycle = Lifecycle(operation[2], failure_at=failure_at)
    with pytest.raises(OperationFailure) as caught:
        invoke(monkeypatch, lifecycle, operation)
    assert caught.value is lifecycle.failure
    expected = ["connect", "begin", "gate"]
    if failure_at != "gate":
        stages = ["bind-bookmarks", "bind-groups", "dml", "repository", "commit"]
        expected.extend(stages[: stages.index(failure_at) + 1])
    assert lifecycle.events == [*expected, "rollback", "close"]


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda item: item[0])
def test_bookmark_gate_conflict_never_calls_repository_or_retries(
    monkeypatch, operation
):
    lifecycle = Lifecycle(operation[2], failure_at="gate-conflict")
    with pytest.raises(domain.BookmarkMutationConflict):
        invoke(monkeypatch, lifecycle, operation)
    assert lifecycle.events == ["connect", "begin", "gate", "rollback", "close"]


@pytest.mark.parametrize("failure_at", ["gate", "repository", "commit"])
@pytest.mark.parametrize("cleanup_failure", ["rollback", "close"])
def test_url_cleanup_failure_does_not_replace_original_exception(
    monkeypatch, failure_at, cleanup_failure
):
    lifecycle = Lifecycle(
        OPERATIONS[0][2], failure_at=failure_at, cleanup_failure=cleanup_failure
    )
    with pytest.raises(OperationFailure) as caught:
        invoke(monkeypatch, lifecycle, OPERATIONS[0])
    assert caught.value is lifecycle.failure
    assert lifecycle.events[-2:] == ["rollback", "close"]
