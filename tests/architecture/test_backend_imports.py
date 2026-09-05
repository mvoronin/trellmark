"""Exercise the committed architecture with the real Import Linter CLI."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORES = [
    f"{feature}.{layer}"
    for feature in ("bookmarks", "backup", "identity")
    for layer in ("domain", "application")
]


def run_contracts(root):
    return subprocess.run(
        [str(Path(sys.executable).with_name("lint-imports")), "--no-cache"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root), "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_repository_import_contracts_pass():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]
    assert config.get("importlinter", {}).get("contracts"), (
        "The repository must declare executable import contracts"
    )
    for contract in config["importlinter"]["contracts"]:
        assert not contract.get("ignore_imports")
        assert not contract.get("exhaustive_ignores")
    result = run_contracts(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout


@pytest.fixture
def contract_fixture(tmp_path):
    # Copy source and the actual configuration, so a weakened contract makes
    # the negative tests fail. No source is imported or executed in this tree.
    shutil.copytree(
        ROOT / "trellmark",
        tmp_path / "trellmark",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    result = run_contracts(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


def assert_rejected(root, importer, imported, contract):
    path = root / "trellmark" / (importer.replace(".", "/") + ".py")
    with path.open("a") as file:
        file.write(f"\nimport {imported}\n")
    result = run_contracts(root)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert f"{contract} BROKEN" in output, output
    assert imported in output, output


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize(
    "framework",
    ["fastapi", "starlette", "pydantic", "sqlalchemy", "psycopg", "aiohttp"],
)
def test_feature_cores_reject_frameworks(contract_fixture, core, framework):
    assert_rejected(contract_fixture, core, framework, "Framework-free feature cores")


@pytest.mark.parametrize(
    ("importer", "imported", "contract"),
    [
        (
            f"{feature}.{lower}",
            f"trellmark.{feature}.{upper}",
            f"{feature} inward layers",
        )
        for feature in ("bookmarks", "backup", "identity")
        for lower, upper in (("domain", "application"), ("application", "persistence"))
    ]
    + [
        ("platform.runtime", f"trellmark.{feature}.domain", "Product-neutral platform")
        for feature in ("bookmarks", "backup", "identity")
    ]
    + [
        ("bookmarks.api", "trellmark.backup.domain", "Bookmarks feature independence"),
        (
            "bookmarks.api",
            "trellmark.identity.domain",
            "Bookmarks feature independence",
        ),
        ("identity.api", "trellmark.bookmarks.domain", "Identity feature independence"),
        ("identity.api", "trellmark.backup.domain", "Identity feature independence"),
        ("backup.api", "trellmark.identity.domain", "Backup excludes Identity"),
        (
            "backup.application",
            "trellmark.bookmarks.application",
            "Backup narrow surface",
        ),
        ("backup.api", "trellmark.bookmarks.integrations", "Backup narrow surface"),
        ("backup.api", "trellmark.bookmarks.backup", "Protected Backup contributor"),
        ("bookmarks.api", "trellmark.bookmarks.backup", "Protected Backup contributor"),
        (
            "backup.api",
            "trellmark.bookmarks.persistence",
            "Protected Bookmark persistence",
        ),
        ("backup.persistence", "trellmark.bookmarks.api", "Protected Bookmark API"),
        ("bookmarks.domain", "trellmark", "No broad package facade imports"),
        ("bookmarks.api", "trellmark", "No broad package facade imports"),
        ("platform.runtime", "trellmark", "No broad package facade imports"),
    ],
)
def test_forbidden_edges_fail_real_cli(contract_fixture, importer, imported, contract):
    assert_rejected(contract_fixture, importer, imported, contract)


@pytest.mark.parametrize(
    "legacy",
    [
        "handlers",
        "storage",
        "models",
        "storage_types",
        "app_keys",
        "page_titles",
        "site_icons",
        "responses",
        "url_normalization",
        "title_text",
        "identity.repository",
        "identity.policy",
    ],
)
def test_reintroduced_legacy_import_fails_real_cli(contract_fixture, legacy):
    path = contract_fixture / "trellmark" / (legacy.replace(".", "/") + ".py")
    path.write_text('"""Synthetic legacy module for architecture regression."""\n')
    assert_rejected(
        contract_fixture,
        "bookmarks.api",
        f"trellmark.{legacy}",
        "No legacy module imports",
    )
