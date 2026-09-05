"""Prove frontend boundaries through the pinned native compiler and real CLI."""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/check_frontend_imports.cjs"


def run_check(root):
    return subprocess.run(
        ["node", str(CHECKER), "--root", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.fixture
def source_tree(tmp_path):
    shutil.copytree(ROOT / "web/src", tmp_path / "web/src")
    shutil.copyfile(ROOT / "tsconfig.json", tmp_path / "tsconfig.json")
    return tmp_path


def add_source(root, name, content):
    path = root / "web/src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def assert_rejected(root, importer, target, rule):
    result = run_check(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"web/src/{importer} -> {target} [{rule}]" in result.stderr
    return result.stderr


def test_pinned_native_parser_accepts_production():
    result = run_check(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeScript 7.0.2" in result.stdout


def test_unimported_feature_to_shell_is_rejected(source_tree):
    add_source(
        source_tree, "features/bookmarks/probe.ts", 'import "../../shell/view.js";'
    )
    assert_rejected(
        source_tree,
        "features/bookmarks/probe.ts",
        "web/src/shell/view.ts",
        "feature-to-shell",
    )


@pytest.mark.parametrize(
    ("name", "source", "target", "rule"),
    [
        ("unknown.ts", "export {};", "web/src/unknown.ts", "unknown-owner"),
        ("shared/broken.ts", "export const = ;", "<syntax>", "parse-error"),
        (
            "shared/missing.ts",
            'import "./absent.js";',
            "web/src/shared/absent.ts",
            "unresolved-target",
        ),
    ],
)
def test_invalid_sources_fail_closed(source_tree, name, source, target, rule):
    add_source(source_tree, name, source)
    assert_rejected(source_tree, name, target, rule)


def test_missing_project_fails_closed(tmp_path):
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "[invalid-project]" in result.stderr


def test_repeated_and_parallel_snapshots_are_stable(source_tree):
    add_source(source_tree, "features/bookmarks/z.ts", 'import "../../shell/view.js";')
    add_source(source_tree, "features/bookmarks/a.ts", 'import "../../shell/view.js";')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run_check, [source_tree] * 3))
    results.extend(run_check(source_tree) for _ in range(2))
    assert all(result.returncode == 1 for result in results)
    assert len({result.stderr for result in results}) == 1
    assert results[0].stderr.splitlines() == sorted(results[0].stderr.splitlines())


@pytest.mark.parametrize(
    "syntax",
    [
        'import { createShellView } from "../../shell/view.js";',
        'import "../../shell/view.js";',
        'import type { X } from "../../shell/view.js";',
        'import { type X } from "../../shell/view.js";',
        'export { createShellView } from "../../shell/view.js";',
        'export * from "../../shell/view.js";',
        'export type { X } from "../../shell/view.js";',
        'type T = import("../../shell/view.js").X;',
        'const p = import("../../shell/view.js");',
        "const p = import(`../../shell/view.js`);",
    ],
)
def test_every_import_form_obeys_ownership(source_tree, syntax):
    add_source(source_tree, "features/bookmarks/probe.ts", syntax)
    assert_rejected(
        source_tree,
        "features/bookmarks/probe.ts",
        "web/src/shell/view.ts",
        "feature-to-shell",
    )
    add_source(
        source_tree,
        "features/bookmarks/probe.ts",
        syntax.replace("../../shell/view.js", "./view.js"),
    )
    result = run_check(source_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("importer", "target", "rule"),
    [
        ("shared/probe.ts", "api/client.ts", "shared-only"),
        ("api/probe.ts", "shell/view.ts", "api-direction"),
        ("shell/probe.ts", "features/bookmarks/view.ts", "shell-to-feature"),
        ("features/notes/probe.ts", "features/bookmarks/model.ts", "cross-feature"),
        ("features/bookmarks/probe.ts", "generated/openapi.d.ts", "generated-private"),
        ("shell/probe.ts", "generated/openapi.d.ts", "generated-private"),
        ("shared/probe.ts", "generated/openapi.d.ts", "generated-private"),
        ("main.ts", "generated/openapi.d.ts", "generated-private"),
        ("design/main.ts", "generated/openapi.d.ts", "generated-private"),
        ("shell/probe.ts", "design/helper.ts", "production-to-design"),
        ("main.ts", "shell/session.ts", "main-public-only"),
        ("main.ts", "features/bookmarks/model.ts", "main-public-only"),
        ("design/helper.ts", "shell/index.ts", "design-isolated"),
        ("design/helper.ts", "features/bookmarks/view.ts", "design-isolated"),
        ("design/main.ts", "features/bookmarks/model.ts", "design-public-only"),
        ("design/main.ts", "shell/session.ts", "design-public-only"),
        ("design/helper.ts", "api/client.ts", "design-isolated"),
        ("design/main.ts", "api/client.ts", "design-public-only"),
        ("shell/probe.ts", "main.ts", "entry-private"),
    ],
)
def test_named_forbidden_edges(source_tree, importer, target, rule):
    import posixpath

    if target == "design/helper.ts":
        add_source(source_tree, target, "export {};")
    specifier = (
        posixpath.relpath(target, posixpath.dirname(importer))
        .removesuffix(".d.ts")
        .removesuffix(".ts")
        + ".js"
    )
    if not specifier.startswith("."):
        specifier = "./" + specifier
    keyword = "import type { X } from" if target.startswith("generated/") else "import"
    add_source(source_tree, importer, f'{keyword} "{specifier}";')
    assert_rejected(source_tree, importer, f"web/src/{target}", rule)


@pytest.mark.parametrize(
    ("source", "target", "rule"),
    [
        ('import "./request";', "./request", "js-suffix"),
        ('import type { X } from "./request.ts";', "./request.ts", "js-suffix"),
        ('export * from "./request";', "./request", "js-suffix"),
        ('type X = import("./request").X;', "./request", "js-suffix"),
        ('import("./request");', "./request", "js-suffix"),
        ('import "alias/request.js";', "alias/request.js", "relative-only"),
        (
            'import "/web/src/shared/request.js";',
            "/web/src/shared/request.js",
            "relative-only",
        ),
        ('import "../../../outside.js";', "outside.ts", "source-root-escape"),
        ('import("./" + name);', "<computed>", "computed-import"),
        ("import(`./${name}.js`);", "<computed>", "computed-import"),
        ('require("../shell/view.js");', "../shell/view.js", "commonjs-forbidden"),
        ("require(name);", "<computed>", "commonjs-forbidden"),
        ("const load = require; load(name);", "<require>", "commonjs-forbidden"),
        ("module.require(name);", "<require>", "commonjs-forbidden"),
        ('globalThis["require"](name);', "<require>", "commonjs-forbidden"),
        (
            'import view = require("../shell/view.js");',
            "../shell/view.js",
            "commonjs-forbidden",
        ),
        ("import view = namespace.view;", "<import-equals>", "commonjs-forbidden"),
        (
            '/// <reference path="../shell/view.ts" />\nexport {};',
            "../shell/view.ts",
            "reference-forbidden",
        ),
    ],
)
def test_syntax_cannot_escape_graph(source_tree, source, target, rule):
    add_source(source_tree, "shared/probe.ts", source)
    assert_rejected(source_tree, "shared/probe.ts", target, rule)


def test_exact_design_entry_and_allowed_type_aliases(source_tree):
    add_source(
        source_tree,
        "design/helper.ts",
        'import type { BookmarkGroup } from "../api/client.js"; export {};',
    )
    add_source(
        source_tree,
        "design/main.ts",
        "\n".join(
            [
                'import "../features/bookmarks/index.js";',
                'import "../features/bookmarks/view.js";',
                'import "../shell/index.js";',
                'import "../shell/view.js";',
                'import "../shared/dom.js";',
                'import "./helper.js";',
            ]
        ),
    )
    result = run_check(source_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "syntax",
    [
        'import type { X } from "../api/client.js";',
        'import { type X } from "../api/client.js";',
        'export type { X } from "../api/client.js";',
        'type X = import("../api/client.js").X;',
    ],
)
def test_design_type_forms_are_allowed(source_tree, syntax):
    add_source(source_tree, "design/helper.ts", syntax)
    result = run_check(source_tree)
    assert result.returncode == 0, result.stderr


def test_generated_declarations_cannot_facade_implementation(source_tree):
    add_source(
        source_tree,
        "generated/facade.d.ts",
        'export type { X } from "../shell/view.js";',
    )
    assert_rejected(
        source_tree,
        "generated/facade.d.ts",
        "web/src/shell/view.ts",
        "generated-no-imports",
    )


def test_generated_access_must_be_type_only(source_tree):
    add_source(source_tree, "api/probe.ts", 'import "../generated/openapi.js";')
    assert_rejected(
        source_tree,
        "api/probe.ts",
        "web/src/generated/openapi.d.ts",
        "declaration-type-only",
    )


def test_generated_implementation_is_rejected(source_tree):
    add_source(source_tree, "generated/probe.ts", "export const x = 1;")
    assert_rejected(
        source_tree,
        "generated/probe.ts",
        "web/src/generated/probe.ts",
        "generated-declarations-only",
    )


def test_transitive_production_design_reachability(source_tree):
    add_source(source_tree, "design/helper.ts", "export {};")
    add_source(source_tree, "shared/bridge.ts", 'export * from "../design/helper.js";')
    add_source(source_tree, "main.ts", 'import "./shared/bridge.js";')
    assert_rejected(
        source_tree,
        "shared/bridge.ts",
        "web/src/design/helper.ts",
        "production-to-design",
    )


def test_excluded_but_imported_dependency_is_inspected(source_tree):
    import json

    config = source_tree / "tsconfig.json"
    data = json.loads(config.read_text())
    data["exclude"] = ["web/src/shared/bridge.ts"]
    config.write_text(json.dumps(data))
    add_source(source_tree, "shared/bridge.ts", 'import "../shell/view.js";')
    add_source(source_tree, "shared/probe.ts", 'import "./bridge.js";')
    assert_rejected(
        source_tree, "shared/bridge.ts", "web/src/shell/view.ts", "shared-only"
    )


def test_symlink_cannot_disguise_target_owner(source_tree):
    (source_tree / "web/src/shared/bridge.ts").symlink_to("../shell/view.ts")
    add_source(source_tree, "shared/probe.ts", 'import "./bridge.js";')
    result = run_check(source_tree)
    assert result.returncode == 1
    assert "[symlink-source]" in result.stderr


@pytest.mark.parametrize(
    "config",
    [
        '{"compilerOptions":',
        '{"files":[]}',
        '{"compilerOptions":{"unknownOption":true},"include":["web/src/**/*.ts"]}',
    ],
)
def test_invalid_project_configuration(source_tree, config):
    (source_tree / "tsconfig.json").write_text(config)
    result = run_check(source_tree)
    assert result.returncode == 1
    assert "[invalid-project]" in result.stderr


def test_same_gate_is_wired_locally_and_in_ci():
    import json

    scripts = json.loads((ROOT / "package.json").read_text())["scripts"]
    assert scripts["check:imports"] == "node scripts/check_frontend_imports.cjs"
    justfile = (ROOT / "justfile").read_text()
    assert "check-frontend-imports:\n    npm run check:imports" in justfile
    assert "check-frontend-imports" in next(
        line for line in justfile.splitlines() if line.startswith("check:")
    )
    assert "npm run check:imports" in (ROOT / ".github/workflows/ci.yml").read_text()
    assert "check-imports:\n    uv run lint-imports" in justfile


def test_public_checker_calls_have_independent_lifetimes(source_tree):
    result = subprocess.run(
        [
            "node",
            "-e",
            "const {checkFrontendImports} = require(process.argv[1]); "
            "Promise.all([1, 2, 3].map(() => checkFrontendImports({root: process.argv[2]})))"
            ".then(results => {if (results.some(r => r.length)) process.exitCode = 1; "
            "else console.log('isolated snapshots closed');});",
            str(CHECKER),
            str(source_tree),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "isolated snapshots closed"
    assert not result.stderr


def test_parent_traversal_cannot_disguise_owner(source_tree):
    add_source(source_tree, "shared/probe.ts", 'import "../shared/../shell/view.js";')
    assert_rejected(
        source_tree, "shared/probe.ts", "web/src/shell/view.ts", "shared-only"
    )


def test_ambient_module_alias_is_rejected(source_tree):
    add_source(
        source_tree,
        "shared/alias.d.ts",
        'declare module "alias" { export type X = string; }',
    )
    assert_rejected(
        source_tree, "shared/alias.d.ts", "alias", "ambient-module-forbidden"
    )


def test_literal_imports_in_comments_and_strings_are_not_edges(source_tree):
    add_source(
        source_tree,
        "shared/probe.ts",
        '// import "../shell/view.js";\nconst text = \'require("shell")\';',
    )
    result = run_check(source_tree)
    assert result.returncode == 0, result.stderr
