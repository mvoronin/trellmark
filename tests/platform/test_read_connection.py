import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from trellmark.bookmarks.persistence import PostgresGroupQueries, PostgresURLQueries
from trellmark.identity.persistence import PostgresIdentityQueries
from trellmark.platform.runtime import get_engine


@pytest.mark.parametrize(
    "query",
    [
        lambda engine: PostgresURLQueries(lambda: engine).list_urls(),
        lambda engine: PostgresURLQueries(lambda: engine).url_by_id(1),
        lambda engine: PostgresURLQueries(lambda: engine).url_by_url(
            "https://example.com"
        ),
        lambda engine: PostgresURLQueries(lambda: engine).url_group_ids(1),
        lambda engine: PostgresGroupQueries(lambda: engine).list_groups(),
        lambda engine: PostgresGroupQueries(lambda: engine).group_by_name("Default"),
        lambda engine: PostgresIdentityQueries(lambda: engine).seeded_identity(),
        lambda engine: PostgresIdentityQueries(lambda: engine).database_ready(),
    ],
)
def test_read_query_preserves_primary_error_when_cleanup_fails(database, query):
    engine = get_engine()
    primary = SQLAlchemyError("primary query failure")
    secondary = OperationalError("rollback", None, RuntimeError("cleanup failure"))
    connections = []

    def fail_query(connection, *args):
        connections.append(connection)
        raise primary

    def fail_rollback(connection):
        raise secondary

    event.listen(engine, "before_cursor_execute", fail_query)
    event.listen(engine, "rollback", fail_rollback)
    try:
        with pytest.raises(SQLAlchemyError) as caught:
            query(engine)
        assert caught.value is primary
    finally:
        event.remove(engine, "before_cursor_execute", fail_query)
        event.remove(engine, "rollback", fail_rollback)
        for connection in connections:
            connection.close()
