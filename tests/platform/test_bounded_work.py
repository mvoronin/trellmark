"""Event-driven worker proofs; timeouts only prevent a broken test from hanging."""

import asyncio
import threading

import pytest
from anyio import (
    CancelScope,
    CapacityLimiter,
    Event,
    create_task_group,
    fail_after,
    from_thread,
    wait_all_tasks_blocked,
)

from tests.helpers import run_async
from trellmark.platform import runtime

CAPACITIES = (
    (runtime.BOOKMARKS_WORK_CAPACITY, 4),
    (runtime.BOOKMARKS_DERIVED_WORK_CAPACITY, 2),
    (runtime.IDENTITY_WORK_CAPACITY, 2),
    (runtime.BACKUP_WORK_CAPACITY, 1),
)


def test_workload_budget_reserves_one_database_checkout():
    assert tuple(actual for actual, _ in CAPACITIES) == (4, 2, 2, 1)
    assert sum(actual for actual, _ in CAPACITIES) == 9
    assert runtime.DATABASE_POOL_SIZE + runtime.DATABASE_MAX_OVERFLOW == 10
    assert runtime.DATABASE_POOL_CAPACITY == 10


@pytest.mark.parametrize(
    "capacity,expected", CAPACITIES, ids=["bookmarks", "derived", "identity", "backup"]
)
def test_each_limiter_saturates_without_blocking_the_event_loop(capacity, expected):
    async def exercise():
        runner = runtime.AnyIOWorkRunner(CapacityLimiter(capacity))
        entered = [Event() for _ in range(expected + 1)]
        releases = [threading.Event() for _ in entered]
        finished = []
        event_loop_thread = threading.get_ident()

        def work(index):
            assert threading.get_ident() != event_loop_thread
            from_thread.run_sync(entered[index].set)
            assert releases[index].wait(3), "worker release watchdog expired"
            return index

        async def invoke(index):
            finished.append(await runner.run(lambda: work(index)))

        with fail_after(3):
            async with create_task_group() as tasks:
                try:
                    for index in range(expected):
                        tasks.start_soon(invoke, index)
                    for started in entered[:-1]:
                        await started.wait()
                    tasks.start_soon(invoke, expected)
                    await wait_all_tasks_blocked()
                    assert runner.limiter.borrowed_tokens == expected
                    assert runner.limiter.statistics().tasks_waiting == 1
                    assert not entered[-1].is_set()
                    assert finished == []

                    # A separate coroutine runs while every worker is blocked.
                    progressed = Event()

                    async def progress():
                        progressed.set()

                    tasks.start_soon(progress)
                    await progressed.wait()
                    assert not entered[-1].is_set()
                    releases[0].set()
                    await entered[-1].wait()
                    assert all(event.is_set() for event in entered)
                finally:
                    for release in releases:
                        release.set()
        assert sorted(finished) == list(range(expected + 1))
        assert runner.limiter.borrowed_tokens == 0

    # Playwright's synchronous fixture may retain an asyncio context on pytest's
    # main thread; use the repository's isolated event-loop helper after browsers.
    run_async(exercise)


@pytest.mark.parametrize("fails", [False, True])
def test_native_cancellation_keeps_capacity_until_worker_cleanup(fails):
    async def exercise():
        runner = runtime.AnyIOWorkRunner(CapacityLimiter(1))
        started = Event()
        release = threading.Event()
        cleanup = threading.Event()
        failure = RuntimeError("synthetic worker failure")

        def work():
            from_thread.run_sync(started.set)
            try:
                assert release.wait(3), "worker release watchdog expired"
                if fails:
                    raise failure
                return 42
            finally:
                cleanup.set()

        task = asyncio.create_task(runner.run(work))
        try:
            await started.wait()
            for _ in range(2):
                task.cancel()
                await wait_all_tasks_blocked()
                assert not task.done()
                assert not cleanup.is_set()
                assert runner.limiter.borrowed_tokens == 1
        finally:
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert cleanup.is_set()
        assert runner.limiter.borrowed_tokens == 0
        if fails:
            assert result is failure
        else:
            assert isinstance(result, asyncio.CancelledError)

    run_async(exercise)


def test_native_cancellation_before_admission_does_not_start_work():
    async def exercise():
        runner = runtime.AnyIOWorkRunner(CapacityLimiter(1))
        entered = threading.Event()
        async with runner.limiter:
            task = asyncio.create_task(runner.run(entered.set))
            await wait_all_tasks_blocked()
            assert runner.limiter.statistics().tasks_waiting == 1
            task.cancel()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
        await wait_all_tasks_blocked()
        assert not entered.is_set()
        assert runner.limiter.borrowed_tokens == 0

    run_async(exercise)


@pytest.mark.parametrize("fails", [False, True], ids=["result", "exception"])
def test_cancellation_observes_worker_completion_and_original_exception(
    monkeypatch, fails
):
    original_run_sync = runtime.to_thread.run_sync
    failure = RuntimeError("synthetic worker failure")
    result = object()

    async def exercise():
        limiter = CapacityLimiter(1)
        started, finished = Event(), Event()
        release = threading.Event()
        cleanup = threading.Event()
        observed = []
        calls = []

        async def run_sync(work, **kwargs):
            calls.append(kwargs)
            return await original_run_sync(work, **kwargs)

        monkeypatch.setattr(runtime.to_thread, "run_sync", run_sync)

        def work():
            from_thread.run_sync(started.set)
            try:
                assert release.wait(3), "worker release watchdog expired"
                if fails:
                    raise failure
                return result
            finally:
                cleanup.set()

        scope = CancelScope()

        async def invoke():
            with scope:
                try:
                    observed.append(await runtime.AnyIOWorkRunner(limiter).run(work))
                except RuntimeError as error:
                    observed.append(error)
                finally:
                    assert cleanup.is_set()
                    finished.set()

        with fail_after(3):
            async with create_task_group() as tasks:
                try:
                    tasks.start_soon(invoke)
                    await started.wait()
                    scope.cancel()
                    await wait_all_tasks_blocked()
                    assert scope.cancel_called
                    assert not finished.is_set()
                    assert not cleanup.is_set()
                    assert limiter.borrowed_tokens == 1
                    assert len(calls) == 1
                    assert calls[0]["abandon_on_cancel"] is False
                    assert calls[0]["limiter"].total_tokens == float("inf")
                finally:
                    release.set()
                await finished.wait()
        assert len(observed) == 1
        assert observed[0] is (failure if fails else result)
        assert limiter.borrowed_tokens == 0

    run_async(exercise)
