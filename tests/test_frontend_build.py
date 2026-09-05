import errno
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, request

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/frontend_build.cjs"
CHECK = ROOT / "scripts/check_frontend_artifacts.cjs"


@pytest.fixture
def frontend_tree(tmp_path):
    config = json.loads((ROOT / "tsconfig.json").read_text())
    (tmp_path / "tsconfig.json").write_text(json.dumps(config))
    (tmp_path / "node_modules").symlink_to(
        ROOT / "node_modules", target_is_directory=True
    )
    source = tmp_path / "web/src/nested"
    source.mkdir(parents=True)
    (source / "value.ts").write_text("export const value: number = 42;\n")
    return tmp_path


def command(script, root, *args):
    return subprocess.run(
        ["node", str(script), "--root", str(root), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def successful(result):
    assert result.returncode == 0, result.stdout + result.stderr


def snapshot(directory):
    return {
        path.relative_to(directory).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_unimported_nested_module_emits_loadable_es_module(frontend_tree):
    successful(command(BUILD, frontend_tree))
    emitted = frontend_tree / "web/static/nested/value.js"
    assert emitted.is_file()
    # Existing frontend tests retain a session-wide sync Playwright context on
    # the main thread. This backend-free smoke must own an independent loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(load_browser_module, emitted).result(timeout=15) == 42
    successful(command(CHECK, frontend_tree))


def load_browser_module(emitted):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route(
            "http://frontend.test/",
            lambda route: route.fulfill(
                content_type="text/html", body="<!doctype html>"
            ),
        )
        page.route(
            "http://frontend.test/nested/value.js",
            lambda route: route.fulfill(
                content_type="text/javascript", body=emitted.read_text()
            ),
        )
        page.goto("http://frontend.test/")
        value = page.evaluate("async () => (await import('./nested/value.js')).value")
        browser.close()
        return value


def test_empty_source_configuration_fails(frontend_tree):
    (frontend_tree / "web/src/nested/value.ts").unlink()
    result = command(BUILD, frontend_tree)
    assert result.returncode != 0
    assert "No inputs" in result.stdout + result.stderr
    assert not (frontend_tree / "web/static").exists()


def test_equal_bytes_keep_distinct_paths_and_timestamps(frontend_tree):
    source = frontend_tree / "web/src/nested"
    (source / "other.ts").write_bytes((source / "value.ts").read_bytes())
    output = frontend_tree / "web/static"
    successful(command(BUILD, frontend_tree))
    before = snapshot(output)
    assert set(before) == {"nested/value.js", "nested/other.js"}
    successful(command(BUILD, frontend_tree))
    successful(command(CHECK, frontend_tree))
    assert snapshot(output) == before


def test_compiler_failure_does_not_publish(frontend_tree):
    successful(command(BUILD, frontend_tree))
    before = snapshot(frontend_tree / "web/static")
    (frontend_tree / "web/src/nested/value.ts").write_text(
        'export const value: number = "wrong";\n'
    )
    result = command(BUILD, frontend_tree)
    assert result.returncode != 0
    assert "not assignable" in result.stdout + result.stderr
    assert snapshot(frontend_tree / "web/static") == before


def test_explicit_output_root_is_independent(frontend_tree, tmp_path):
    output = tmp_path / "separate"
    successful(command(BUILD, frontend_tree, "--output-root", output))
    assert (output / "static/nested/value.js").is_file()
    assert not (frontend_tree / "web/static").exists()
    successful(command(CHECK, frontend_tree, "--output-root", output))


@pytest.mark.parametrize(
    "fault", ["corrupt", "missing", "extra", "unknown", "delete", "rename"]
)
def test_drift_is_rejected_without_repair(frontend_tree, fault):
    source = frontend_tree / "web/src/nested/value.ts"
    (source.parent / "remaining.ts").write_text("export const remaining = true;\n")
    successful(command(BUILD, frontend_tree))
    output = frontend_tree / "web/static"
    if fault == "corrupt":
        (output / "nested/value.js").write_text("corrupt")
    elif fault == "missing":
        (output / "nested/value.js").unlink()
    elif fault in {"extra", "unknown"}:
        (output / "untracked").mkdir()
        (
            output / "untracked" / ("extra.js" if fault == "extra" else "extra.bin")
        ).write_bytes(b"extra")
    elif fault == "delete":
        source.unlink()
    else:
        source.rename(source.with_name("renamed.ts"))
    before = snapshot(output)
    result = command(CHECK, frontend_tree)
    assert result.returncode == 1
    assert "artifact" in result.stderr or "different bytes" in result.stderr
    assert snapshot(output) == before
    successful(command(BUILD, frontend_tree))
    successful(command(CHECK, frontend_tree))


def test_diagnostics_are_sorted_by_complete_relative_path(frontend_tree):
    successful(command(BUILD, frontend_tree))
    output = frontend_tree / "web/static"
    for relative in ["z.js", "a/z.js", "a.js", "a/b.bin"]:
        artifact = output / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"extra")
    (output / "nested/value.js").unlink()
    result = command(CHECK, frontend_tree)
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "static/a.js: unexpected artifact",
        "static/a/b.bin: unexpected artifact",
        "static/a/z.js: unexpected artifact",
        "static/nested/value.js: missing artifact",
        "static/z.js: unexpected artifact",
    ]


def test_maintained_assets_survive_and_generated_overlap_fails(frontend_tree):
    output = frontend_tree / "web/static"
    for relative in [
        "styles.css",
        "icons.svg",
        "favicon.svg",
        "favicon.ico",
        "apple-touch-icon.png",
        "fonts/nested/local.woff2",
    ]:
        asset = output / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"maintained")
    before = snapshot(output)
    successful(command(BUILD, frontend_tree))
    successful(command(CHECK, frontend_tree))
    assert {key: snapshot(output)[key] for key in before} == before
    source = frontend_tree / "web/src/fonts"
    source.mkdir()
    (source / "collision.ts").write_text("export const collision = true;\n")
    generated_before = snapshot(output)
    result = command(BUILD, frontend_tree)
    assert result.returncode == 1
    assert "overlaps maintained asset: static/fonts/collision.js" in result.stderr
    assert snapshot(output) == generated_before


def test_parallel_checks_are_isolated(frontend_tree):
    successful(command(BUILD, frontend_tree))
    before = snapshot(frontend_tree / "web/static")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: command(CHECK, frontend_tree), range(4)))
    for result in results:
        successful(result)
    assert snapshot(frontend_tree / "web/static") == before


def publication_process(frontend_tree, behavior):
    hook = frontend_tree / "publication_hook.cjs"
    output = str(frontend_tree / "web/static") + os.sep
    hook.write_text(
        'const fs = require("node:fs");\n'
        "const original = fs.writeFileSync;\n"
        "let intercepted = false;\n"
        "fs.writeFileSync = function(file, ...args) {\n"
        "  const result = original.call(this, file, ...args);\n"
        f"  if (!intercepted && String(file).startsWith({json.dumps(output)})) {{\n"
        "    intercepted = true;\n"
        '    process.stdout.write("publication reached\\n");\n'
        f"    {behavior}\n"
        "  }\n"
        "  return result;\n"
        "};\n"
    )
    return subprocess.Popen(
        ["node", "--require", str(hook), str(BUILD), "--root", str(frontend_tree)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_publishers_exclude_competitors_and_recover_after_kill(frontend_tree):
    source = frontend_tree / "web/src/nested/value.ts"
    successful(command(BUILD, frontend_tree))
    source.write_text("export const value = 43;\n")
    (source.parent / "z.ts").write_text("export const other = 44;\n")
    publisher = publication_process(
        frontend_tree, "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);"
    )
    try:
        assert select.select([publisher.stdout], [], [], 15)[0]
        assert publisher.stdout.readline() == "publication reached\n"
        competing = command(BUILD, frontend_tree)
        assert competing.returncode == 1
        assert "publication already in progress" in competing.stderr
    finally:
        publisher.kill()
        publisher.communicate(timeout=10)
    partial = command(CHECK, frontend_tree)
    assert partial.returncode == 1
    successful(command(BUILD, frontend_tree))
    successful(command(CHECK, frontend_tree))


def test_caught_publication_failure_releases_lock_and_requires_repair(frontend_tree):
    successful(command(BUILD, frontend_tree))
    (frontend_tree / "web/src/nested/value.ts").write_text("export const value = 43;\n")
    publisher = publication_process(
        frontend_tree, 'throw new Error("injected write failure");'
    )
    _, stderr = publisher.communicate(timeout=15)
    assert publisher.returncode == 1
    assert "injected write failure" in stderr
    # Bytes can already match even though publication never completed successfully.
    assert command(CHECK, frontend_tree).returncode == 1
    successful(command(BUILD, frontend_tree))
    successful(command(CHECK, frontend_tree))


def test_output_cannot_overwrite_source_or_follow_symlinks(frontend_tree, tmp_path):
    source = frontend_tree / "web/src"
    before = snapshot(source)
    for output in [frontend_tree, source, source / "nested/generated"]:
        result = command(BUILD, frontend_tree, "--output-root", output)
        assert result.returncode == 1
        assert "Output root must be separate" in result.stderr
    external = tmp_path / "outside"
    external.mkdir()
    output = frontend_tree / "web/static"
    output.symlink_to(external, target_is_directory=True)
    assert command(BUILD, frontend_tree).returncode == 1
    assert snapshot(external) == {}
    assert snapshot(source) == before


def test_local_and_ci_use_the_same_checker_before_any_build():
    recipes = (ROOT / "justfile").read_text()
    recipe = recipes.split("check-frontend-artifacts:\n", 1)[1].split("\n\n", 1)[0]
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "node scripts/check_frontend_artifacts.cjs" in recipe
    assert "node scripts/check_frontend_artifacts.cjs" in ci
    before_check = ci.split("node scripts/check_frontend_artifacts.cjs", 1)[0]
    assert "npm run build" not in before_check
    assert "build-frontend" not in recipe


def test_source_rename_can_replace_an_obsolete_output_directory(frontend_tree):
    source = frontend_tree / "web/src/a.js"
    source.mkdir()
    (source / "child.ts").write_text("export const child = true;\n")
    successful(command(BUILD, frontend_tree))
    (source / "child.ts").unlink()
    source.rmdir()
    (source.parent / "a.ts").write_text("export const replacement = true;\n")
    assert command(CHECK, frontend_tree).returncode == 1
    successful(command(BUILD, frontend_tree))
    assert (frontend_tree / "web/static/a.js").is_file()
    successful(command(CHECK, frontend_tree))


def test_nested_output_symlink_is_rejected_without_touching_target(
    frontend_tree, tmp_path
):
    successful(command(BUILD, frontend_tree))
    external = tmp_path / "private.txt"
    external.write_bytes(b"do not change")
    artifact = frontend_tree / "web/static/nested/value.js"
    artifact.unlink()
    artifact.symlink_to(external)
    for script in [BUILD, CHECK]:
        result = command(script, frontend_tree)
        assert result.returncode == 1
        assert "Unsupported artifact: static/nested/value.js" in result.stderr
    assert external.read_bytes() == b"do not change"


@pytest.mark.parametrize(
    "invalid",
    [
        "h('div', { innerHTML: '<img>' });",
        "h('div', { outerHTML: '<img>' });",
        "h('div', { textContent: 'unsafe API surface' });",
        "h('div', { unknown: true });",
        "h('div', { attributes: { onclick: 'alert(1)' } });",
        "h('button', { on: { click: 'alert(1)' } });",
        "h('input', { value: 4 });",
        "h('input', { checked: 'yes' });",
        "h('div', { href: 'https://example.test' });",
        "h('a', { checked: true });",
        "h('button', { type: 'text' });",
        "h('div', { style: { cssText: 'color:red' } });",
        "h('div', { data: { mystery: 'value' } });",
        "h('div', { aria: { mystery: 'value' } });",
    ],
)
def test_dom_helper_compiler_rejects_unsafe_or_wrong_properties(frontend_tree, invalid):
    source = frontend_tree / "web/src/shared"
    source.mkdir()
    (source / "dom.ts").write_bytes((ROOT / "web/src/shared/dom.ts").read_bytes())
    fixture = source.parent / "fixture.ts"
    fixture.write_text(
        "import { h } from './shared/dom.js';\n"
        "const input: HTMLInputElement = h('input', { value: '', checked: false });\n"
        "const button: HTMLButtonElement = h('button', { type: 'button' });\n"
        "h('button', { on: { click: event => { const click: MouseEvent = event; void click; } } });\n"
        "void input; void button;\n"
    )
    successful(command(BUILD, frontend_tree))
    with fixture.open("a") as file:
        file.write(invalid + "\n")
    result = command(BUILD, frontend_tree)
    assert result.returncode == 1
    assert "fixture.ts" in result.stderr
    assert "error TS" in result.stderr


@pytest.fixture
def html_tree(frontend_tree):
    source = frontend_tree / "web/html"
    (source / "fragments").mkdir(parents=True)
    (source / "index.html").write_text("<main>Original</main>\n")
    return frontend_tree


def test_html_expands_empty_repeated_nested_and_named_fragments(html_tree):
    source = html_tree / "web/html"
    (source / "fragments/empty.html").write_text("")
    (source / "fragments/text.html").write_text("<b>日本語 e\u0301</b>")
    (source / "fragments/nested.html").write_text(
        "<!-- include fragments/text.html -->"
    )
    (source / "fragments/sections.html").write_text(
        "<!-- fragment-start first --><i>First</i><!-- fragment-end first -->"
        "<!-- fragment-start second --><i>Second</i><!-- fragment-end second -->"
    )
    (source / "index.html").write_text(
        "<main><!-- include fragments/empty.html -->"
        "<!-- include fragments/nested.html --><!-- include fragments/text.html -->"
        "<!-- include fragments/sections.html#second -->"
        "<!-- include fragments/sections.html#first --></main>"
    )
    successful(command(BUILD, html_tree))
    assert (html_tree / "web/index.html").read_text() == (
        "<main><b>日本語 e\u0301</b><b>日本語 e\u0301</b><i>Second</i><i>First</i></main>\n"
    )
    before = snapshot(html_tree / "web")
    successful(command(BUILD, html_tree))
    successful(command(CHECK, html_tree))
    assert snapshot(html_tree / "web") == before


@pytest.mark.parametrize("section", [False, True])
def test_html_expands_only_original_include_tokens(html_tree, section):
    source = html_tree / "web/html"
    (source / "fragments/text.html").write_text("<b>included</b>")
    contents = (
        "<!-- ordinary comment -->"
        "<b><!-- include fragments/text.html --></b>"
        "<!<!-- fragment-start empty --><!-- fragment-end empty -->"
        "-- include fragments/text.html -->"
    )
    if section:
        (source / "fragments/sections.html").write_text(
            "<!-- fragment-start content -->"
            + contents.replace(
                "<!-- fragment-start empty --><!-- fragment-end empty -->",
                "<!-- include fragments/empty.html -->",
            )
            + "<!-- fragment-end content -->"
        )
        (source / "fragments/empty.html").write_text("")
        contents = "<!-- include fragments/sections.html#content -->"
    (source / "index.html").write_text(contents)
    successful(command(BUILD, html_tree))
    assert (html_tree / "web/index.html").read_text() == (
        "<!-- ordinary comment --><b><b>included</b></b>"
        "<!-- include fragments/text.html -->\n"
    )
    successful(command(CHECK, html_tree))


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "cycle",
        "section-cycle",
        "escape",
        "symlink",
        "absolute",
        "malformed",
        "unclosed",
        "unclosed-comment",
        "nested-comment",
        "expression",
        "missing-section",
        "duplicate-section",
        "unclosed-section",
        "wrong-end",
        "nested-section",
    ],
)
def test_invalid_html_never_publishes(html_tree, fault):
    source = html_tree / "web/html"
    successful(command(BUILD, html_tree))
    published = (html_tree / "web/index.html").read_bytes()
    before_js = snapshot(html_tree / "web/static")
    outside = html_tree / "outside.html"
    outside.write_text("private fixture data")
    fragment = source / "fragments/test.html"
    fragment.write_text("<p>valid</p>")
    include = "<!-- include fragments/test.html -->"
    if fault == "missing":
        fragment.unlink()
    elif fault == "cycle":
        fragment.write_text("<!-- include index.html -->")
    elif fault == "section-cycle":
        fragment.write_text(
            "<!-- fragment-start a --><!-- include fragments/test.html#b --><!-- fragment-end a --><!-- fragment-start b --><!-- include fragments/test.html#a --><!-- fragment-end b -->"
        )
        include = "<!-- include fragments/test.html#a -->"
    elif fault == "escape":
        include = "<!-- include ../../outside.html -->"
    elif fault == "symlink":
        fragment.unlink()
        fragment.symlink_to(outside)
    elif fault == "absolute":
        include = f"<!-- include {outside} -->"
    elif fault == "malformed":
        include = "<!-- include: fragments/test.html -->"
    elif fault == "unclosed":
        include = "<!-- include fragments/test.html"
    elif fault == "unclosed-comment":
        include = "<!-- ordinary unfinished comment"
    elif fault == "nested-comment":
        include = "<!-- ordinary <!-- include fragments/test.html --> -->"
    elif fault == "expression":
        include = "<!-- include ${process.exit(0)} -->"
    elif fault == "missing-section":
        include = "<!-- include fragments/test.html#missing -->"
    elif fault == "duplicate-section":
        fragment.write_text(
            "<!-- fragment-start a -->one<!-- fragment-end a --><!-- fragment-start a -->two<!-- fragment-end a -->"
        )
    elif fault == "unclosed-section":
        fragment.write_text("<!-- fragment-start a -->one")
    elif fault == "wrong-end":
        fragment.write_text("<!-- fragment-start a -->one<!-- fragment-end b -->")
    else:
        fragment.write_text(
            "<!-- fragment-start a --><!-- fragment-start b -->one<!-- fragment-end b --><!-- fragment-end a -->"
        )
    (source / "index.html").write_text(include)
    result = command(BUILD, html_tree)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "HTML" in result.stderr
    assert "private fixture data" not in result.stderr
    assert (html_tree / "web/index.html").read_bytes() == published
    assert snapshot(html_tree / "web/static") == before_js


@pytest.mark.parametrize("page", ["index", "design"])
@pytest.mark.parametrize("fault", ["missing", "corrupt", "orphan"])
def test_generated_pages_participate_in_drift_checks(html_tree, page, fault):
    source = html_tree / "web/html"
    (source / "design.html").write_text("<main>Design</main>\n")
    successful(command(BUILD, html_tree))
    artifact = html_tree / f"web/{page}.html"
    if fault == "missing":
        artifact.unlink()
    elif fault == "corrupt":
        artifact.write_bytes(b"broken")
    else:
        (source / f"{page}.html").unlink()
    before = snapshot(html_tree / "web")
    result = command(CHECK, html_tree)
    assert result.returncode == 1
    assert f"{page}.html:" in result.stderr
    assert snapshot(html_tree / "web") == before
    successful(command(BUILD, html_tree))
    successful(command(CHECK, html_tree))


def test_html_and_js_share_isolated_output_root(html_tree, tmp_path):
    output = tmp_path / "generated"
    successful(command(BUILD, html_tree, "--output-root", output))
    assert (output / "index.html").read_text() == "<main>Original</main>\n"
    assert (output / "static/nested/value.js").is_file()
    assert not (html_tree / "web/index.html").exists()
    successful(command(CHECK, html_tree, "--output-root", output))
    before = snapshot(output)
    (html_tree / "web/src/nested/value.ts").write_text(
        'export const value: number = "bad";'
    )
    assert command(BUILD, html_tree, "--output-root", output).returncode == 1
    assert snapshot(output) == before


def test_design_frame_css_is_the_only_additional_maintained_asset(html_tree):
    output = html_tree / "web/static/design"
    output.mkdir(parents=True)
    (output / "frame.css").write_text("body { margin: 1rem; }\n")
    before = snapshot(output)
    successful(command(BUILD, html_tree))
    successful(command(CHECK, html_tree))
    assert snapshot(output) == before
    for name in ["extra.css", "extra.js"]:
        (output / name).write_text("unexpected")
    result = command(CHECK, html_tree)
    assert result.returncode == 1
    assert "extra.css" in result.stderr and "extra.js" in result.stderr
    successful(command(BUILD, html_tree))
    assert snapshot(output) == before


@pytest.mark.parametrize("fault", ["corrupt", "missing", "orphan", "unknown"])
def test_complete_design_tree_is_checked_without_repair(html_tree, fault):
    source = html_tree / "web/src/design"
    source.mkdir()
    (source / "main.ts").write_text("export const synthetic = true;\n")
    (html_tree / "web/html/design.html").write_text("<main>Design</main>\n")
    successful(command(BUILD, html_tree))
    artifact = html_tree / "web/static/design/main.js"
    if fault == "corrupt":
        artifact.write_text("broken")
    elif fault == "missing":
        artifact.unlink()
    elif fault == "orphan":
        (source / "main.ts").unlink()
    else:
        (artifact.parent / "unknown.bin").write_bytes(b"unknown")
    before = snapshot(html_tree / "web")
    result = command(CHECK, html_tree)
    assert result.returncode == 1
    assert "static/design/" in result.stderr
    assert snapshot(html_tree / "web") == before
    successful(command(BUILD, html_tree))
    successful(command(CHECK, html_tree))


def test_exact_design_mode_recipe_is_loopback_only_and_releases_port(tmp_path):
    # The developer command intentionally owns a fixed port; unrelated browser
    # fixtures retain ephemeral ports. Never terminate an existing port owner.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8001))
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TRELLMARK_DATABASE_URL", "TRELLMARK_PUBLIC_ORIGIN"}
    }
    env["PYTHONUNBUFFERED"] = "1"
    opener = request.build_opener(request.ProxyHandler({}))
    with (tmp_path / "design-mode.log").open("w+") as log:
        process = subprocess.Popen(
            ["just", "design-mode"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, log.read()
                try:
                    with opener.open(
                        "http://127.0.0.1:8001/design.html", timeout=1
                    ) as response:
                        assert (
                            response.read() == (ROOT / "web/design.html").read_bytes()
                        )
                    break
                except error.URLError:
                    assert time.monotonic() < deadline, (
                        "Design command did not serve within 30s"
                    )
                    time.sleep(0.1)
            with socket.socket() as public_probe:
                public_probe.settimeout(1)
                assert public_probe.connect_ex(("127.0.0.2", 8001)) != 0
            for path in ["/pyproject.toml", "/api/auth/session"]:
                with pytest.raises(error.HTTPError) as failure:
                    opener.open("http://127.0.0.1:8001" + path, timeout=2)
                assert failure.value.code == 404
            conflict = subprocess.run(
                ["just", "design-mode"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert conflict.returncode != 0
            assert "Address already in use" in conflict.stderr
            assert process.poll() is None
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        log.seek(0)
        output = log.read()
        assert "http://127.0.0.1:8001/design.html" in output
        assert "--bind 127.0.0.1 --directory web" in output
    # Reaping just does not reap its descendants. Allow the signalled server
    # child to close its listener, but still fail if the port remains owned.
    deadline = time.monotonic() + 5
    while True:
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                released.bind(("127.0.0.1", 8001))
                break
            except OSError as failure:
                if failure.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                    raise
        time.sleep(0.05)


def test_real_page_reproduces_from_authoritative_html_source(frontend_tree):
    shutil.copytree(ROOT / "web/html", frontend_tree / "web/html")
    successful(command(BUILD, frontend_tree))
    assert (frontend_tree / "web/index.html").read_bytes() == (
        ROOT / "web/index.html"
    ).read_bytes()


class PageContract(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.nodes.append((tag, attributes, tuple(self.stack)))
        if tag not in {"meta", "link", "input", "br", "img", "hr", "use"}:
            self.stack.append((tag, attributes.get("id")))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()


def test_shared_fragments_preserve_application_owners_and_references(frontend_tree):
    source = ROOT / "web/html"
    shell = (source / "fragments/shell.html").read_text()
    dialogs = (source / "fragments/dialogs.html").read_text()
    forms = (source / "fragments/bookmark_forms.html").read_text()
    assert 'id="login-form"' in shell
    assert 'id="logout-button"' in shell
    assert 'id="confirm-dialog"' in dialogs
    assert 'id="group-delete-dialog"' in dialogs
    for name in ["url", "group", "group-edit", "url-edit"]:
        assert f'id="{name}-form"' in forms
        assert f'id="{name}-form"' not in shell + dialogs
    shutil.copytree(source, frontend_tree / "web/html")
    # A second page consumes exactly the same owned source without runtime requests.
    (frontend_tree / "web/html/design.html").write_bytes(
        (source / "index.html").read_bytes()
    )
    successful(command(BUILD, frontend_tree))
    page = (frontend_tree / "web/index.html").read_bytes()
    assert (frontend_tree / "web/design.html").read_bytes() == page
    successful(command(BUILD, frontend_tree))
    assert (frontend_tree / "web/index.html").read_bytes() == page
    assert b"<!-- include" not in page and b"<!-- fragment-" not in page
    contract = PageContract(page.decode())
    ids = [attrs["id"] for _, attrs, _ in contract.nodes if "id" in attrs]
    assert len(ids) == len(set(ids))
    by_id = {
        attrs["id"]: (tag, attrs, parents)
        for tag, attrs, parents in contract.nodes
        if "id" in attrs
    }
    for _, attrs, _ in contract.nodes:
        for reference in [
            "for",
            "aria-labelledby",
            "aria-describedby",
            "aria-controls",
        ]:
            assert all(target in by_id for target in attrs.get(reference, "").split())
    for owner, fields in {
        "login-form": ["login-input", "password-input", "login-button"],
        "url-form": ["url-input", "url-clear", "save-button", "form-status"],
        "group-form": [
            "group-input",
            "group-domains",
            "group-parent",
            "group-nsfw",
            "group-button",
            "group-status",
        ],
        "group-edit-form": [
            "group-edit-name",
            "group-edit-nsfw",
            "group-edit-domains",
            "group-edit-parent",
            "group-edit-status",
            "group-edit-cancel",
            "group-edit-save",
        ],
        "url-edit-form": [
            "url-edit-title",
            "url-edit-url",
            "url-edit-status",
            "url-edit-cancel",
            "url-edit-save",
        ],
    }.items():
        positions = [ids.index(field) for field in fields]
        assert positions == sorted(positions)
        for field in fields:
            assert ("form", owner) in by_id[field][2]
    assert ("header", None) in by_id["url-form"][2]
    assert ("main", None) in by_id["group-form"][2]
    for name in ["group", "url"]:
        assert ("dialog", f"{name}-edit-dialog") in by_id[f"{name}-edit-form"][2]
    confirmation_forms = [
        parents[-1][1]
        for tag, attrs, parents in contract.nodes
        if tag == "form" and attrs.get("method") == "dialog"
    ]
    assert confirmation_forms == ["confirm-dialog", "group-delete-dialog"]
    assert by_id["session-pending"][1]["aria-live"] == "polite"
    assert "hidden" in by_id["login-view"][1] and "hidden" in by_id["app-view"][1]
