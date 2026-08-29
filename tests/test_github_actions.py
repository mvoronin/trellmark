"""Security boundaries for the public repository's GitHub Actions workflows."""

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = BASE_DIR / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text()


def test_ci_uses_a_disposable_postgres_service_without_repository_secrets():
    workflow = _workflow("ci.yml")

    assert "image: postgres:18-alpine" in workflow
    assert "TRELLMARK_TEST_DATABASE_URL:" in workflow
    assert "trellmark-ci-only" in workflow
    assert "${{ secrets." not in workflow


def test_ci_cannot_keep_checkout_credentials_or_write_repository_contents():
    workflow = _workflow("ci.yml")

    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "pull_request_target:" not in workflow
    assert "runs-on: self-hosted" not in workflow


def test_publish_uses_only_the_repository_scoped_github_token():
    workflow = _workflow("publish.yml")
    secret_references = set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow))

    assert secret_references == {"GITHUB_TOKEN"}
    assert "packages: write" in workflow
    assert "environment:" not in workflow
    assert "runs-on: self-hosted" not in workflow


def test_publish_builds_one_self_contained_trellmark_image():
    workflow = _workflow("publish.yml")

    assert "images: ghcr.io/${{ github.repository_owner }}/trellmark" in workflow
    assert "file: deploy/Containerfile" in workflow
    assert "matrix." not in workflow
    assert "trellmark-web" not in workflow


def test_ci_has_no_dependency_on_the_external_caddy_deployment():
    workflow = _workflow("ci.yml")

    assert "caddy" not in workflow.lower()


def test_external_actions_are_pinned_to_full_commit_shas():
    for path in WORKFLOW_DIR.glob("*.yml"):
        for line in path.read_text().splitlines():
            value = line.strip()
            if not value.startswith("uses:") or "uses: ./" in value:
                continue
            reference = value.removeprefix("uses:").strip().split()[0]
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
                f"{path.name}: action is not pinned to a commit: {reference}"
            )


def test_docker_context_excludes_private_local_material():
    ignored = (BASE_DIR / ".dockerignore").read_text().splitlines()

    for pattern in (
        ".git/",
        "**/__pycache__/",
        "**/*.py[cod]",
        ".env",
        ".env.*",
        "*.key",
        "*.dump",
        "*.sql",
    ):
        assert pattern in ignored
