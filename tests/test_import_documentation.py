from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
README = BASE_DIR / "README.md"


def _import_export_section() -> str:
    readme = README.read_text()
    heading = "## Import and export"
    assert heading in readme
    section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    assert readme.index(heading) < readme.index("## Backups")
    return section


def test_readme_promises_atomic_bookmark_import_outcomes():
    section = _import_export_section()
    normalized = " ".join(section.split())

    assert "all-or-nothing" in normalized
    assert "duplicate URLs remain successful skips" in normalized
    assert "fails immediately" in normalized
    assert "does not retry automatically" in normalized
    assert "Retry import" in normalized
    assert "bookmark data unchanged" in normalized
    assert "409" in normalized
    assert "import_conflict" in normalized


def test_readme_explains_the_bookmark_write_gate_and_isolation_boundary():
    section = _import_export_section()
    normalized = " ".join(section.split())

    assert "pg_try_advisory_xact_lock" in normalized
    assert "transaction-scoped, non-blocking gate" in normalized
    assert "before data changes begin" in normalized
    assert "commits or rolls back" in normalized
    assert "SERIALIZABLE" in normalized
    assert "serialization failure" in normalized
    assert "transaction-retry handling" in normalized
    assert "would not replace the gate" in normalized
    assert "atomic commit and rollback" in normalized
    assert "constraints enforce" in normalized
    assert "Wave 2" not in normalized
    assert "lock key" not in normalized.lower()
