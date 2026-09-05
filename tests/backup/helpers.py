"""Test-only post-mutation observation through the injected coordinator factory."""

from dataclasses import dataclass

from trellmark.backup.persistence import PostgresBackupUnitOfWorkFactory
from trellmark.platform.runtime import get_engine


class ObservedContributor:
    def __init__(self, contributor, observer):
        self.contributor = contributor
        self.observer = observer

    def __getattr__(self, name):
        operation = getattr(self.contributor, name)
        stages = {
            "restore_group": "group_metadata",
            "detach_group": "hierarchy_detach",
            "attach_group": "hierarchy_attach",
            "reorder_siblings": "sibling_order",
            "insert_url": "url_insert",
            "restore_membership": "membership_insert",
            "restore_url_metadata": "url_metadata",
        }
        if name not in stages:
            return operation

        def observed(*args, **kwargs):
            result = operation(*args, **kwargs)
            # A duplicate URL performs no mutation and must not count as an
            # inserted URL occurrence in the original Phase 1 stage matrix.
            if name != "insert_url" or result is not None:
                self.observer(stages[name])
            return result

        return observed


class ObservedBackupUnitOfWork:
    def __init__(self, inner, observer):
        self.inner = inner
        self.observer = observer

    def __enter__(self):
        self.inner.__enter__()
        self.bookmarks = ObservedContributor(self.inner.bookmarks, self.observer)
        return self

    def commit(self):
        self.inner.commit()

    def __exit__(self, *args):
        return self.inner.__exit__(*args)


@dataclass
class ObservedBackupFactory:
    observer: object = None

    def __call__(self):
        inner = PostgresBackupUnitOfWorkFactory(get_engine)()
        if self.observer is None:
            return inner
        return ObservedBackupUnitOfWork(inner, self.observer)
