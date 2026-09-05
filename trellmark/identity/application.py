"""Identity use cases own complete worker-local query and write scopes."""

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, TypeVar, assert_never

from .domain import (
    IdentityCleaned,
    LoginCommand,
    LoginCreated,
    LoginFailureRecorded,
    LoginOutcome,
    LoginThrottled,
    PasswordRotated,
    RevokeSessionCommand,
    RotatePasswordCommand,
    SeededIdentityOutcome,
    SessionAuthenticated,
    SessionCommand,
    SessionMissing,
    SessionOutcome,
    SessionRevoked,
    decode_session_secrets,
)

ResultT = TypeVar("ResultT")


class WorkRunner(Protocol):
    async def run(self, work: Callable[[], ResultT]) -> ResultT: ...


class IdentityRepository(Protocol):
    def login(self, command: LoginCommand) -> LoginOutcome: ...
    def authenticate_session(self, command: SessionCommand) -> SessionOutcome: ...
    def rotate_password(self, command: RotatePasswordCommand) -> PasswordRotated: ...
    def revoke_session(self, command: RevokeSessionCommand) -> SessionRevoked: ...
    def cleanup(self) -> IdentityCleaned: ...


class IdentityQueries(Protocol):
    def lookup_session(self, cookie_value: str | None) -> SessionOutcome: ...
    def seeded_identity(self) -> SeededIdentityOutcome: ...
    def database_ready(self) -> bool: ...


class IdentityUnitOfWork(Protocol):
    @property
    def identity(self) -> IdentityRepository: ...

    def __enter__(self) -> "IdentityUnitOfWork": ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class IdentityUnitOfWorkFactory(Protocol):
    def __call__(self) -> IdentityUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class IdentityApplicationService:
    work_runner: WorkRunner
    uow_factory: IdentityUnitOfWorkFactory
    queries: IdentityQueries

    async def login(self, command: LoginCommand) -> LoginOutcome:
        return await self.work_runner.run(
            lambda: login_in_uow(self.uow_factory, command)
        )

    async def authenticate_session(self, command: SessionCommand) -> SessionOutcome:
        return await self.work_runner.run(
            lambda: authenticate_session_with_ports(
                self.uow_factory, self.queries, command
            )
        )

    async def rotate_password(self, command: RotatePasswordCommand) -> PasswordRotated:
        return await self.work_runner.run(
            lambda: rotate_password_in_uow(self.uow_factory, command)
        )

    async def revoke_session(self, command: RevokeSessionCommand) -> SessionRevoked:
        return await self.work_runner.run(
            lambda: revoke_session_in_uow(self.uow_factory, command)
        )

    async def cleanup(self) -> IdentityCleaned:
        return await self.work_runner.run(lambda: cleanup_in_uow(self.uow_factory))

    async def seeded_identity(self) -> SeededIdentityOutcome:
        return await self.work_runner.run(self.queries.seeded_identity)

    async def database_ready(self) -> bool:
        return await self.work_runner.run(self.queries.database_ready)


def login_in_uow(
    factory: IdentityUnitOfWorkFactory, command: LoginCommand
) -> LoginOutcome:
    with factory() as unit_of_work:
        outcome = unit_of_work.identity.login(command)
        match outcome:
            case LoginCreated() | LoginFailureRecorded() | LoginThrottled():
                # Rejection is a successful policy decision: its failed-attempt
                # accounting and bounded housekeeping must remain durable.
                unit_of_work.commit()
                return outcome
        assert_never(outcome)


def authenticate_session_with_ports(
    factory: IdentityUnitOfWorkFactory,
    queries: IdentityQueries,
    command: SessionCommand,
) -> SessionOutcome:
    # Invalid or absent cookies never require a database checkout, including
    # during database outages and idempotent anonymous logout.
    if decode_session_secrets(command.cookie_value) is None:
        return SessionMissing()
    if not command.touch:
        return queries.lookup_session(command.cookie_value)
    return authenticate_session_in_uow(factory, command)


def authenticate_session_in_uow(
    factory: IdentityUnitOfWorkFactory, command: SessionCommand
) -> SessionOutcome:
    with factory() as unit_of_work:
        outcome = unit_of_work.identity.authenticate_session(command)
        match outcome:
            case SessionAuthenticated():
                unit_of_work.commit()
                return outcome
            case SessionMissing():
                return outcome
        assert_never(outcome)


def rotate_password_in_uow(
    factory: IdentityUnitOfWorkFactory, command: RotatePasswordCommand
) -> PasswordRotated:
    with factory() as unit_of_work:
        outcome = unit_of_work.identity.rotate_password(command)
        match outcome:
            case PasswordRotated():
                unit_of_work.commit()
                return outcome
        assert_never(outcome)


def revoke_session_in_uow(
    factory: IdentityUnitOfWorkFactory, command: RevokeSessionCommand
) -> SessionRevoked:
    with factory() as unit_of_work:
        outcome = unit_of_work.identity.revoke_session(command)
        match outcome:
            case SessionRevoked():
                unit_of_work.commit()
                return outcome
        assert_never(outcome)


def cleanup_in_uow(factory: IdentityUnitOfWorkFactory) -> IdentityCleaned:
    with factory() as unit_of_work:
        outcome = unit_of_work.identity.cleanup()
        match outcome:
            case IdentityCleaned():
                unit_of_work.commit()
                return outcome
        assert_never(outcome)
