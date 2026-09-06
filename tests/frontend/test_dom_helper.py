from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers


def test_bookmark_views_are_inert_and_render_repeated_memberships(static_page):
    result = static_page.evaluate(
        """async () => {
          const before = document.body.innerHTML;
          const { renderGroups } = await import('/static/features/bookmarks/view.js');
          const { createBookmarksModel } = await import('/static/features/bookmarks/model.js');
          const inert = before === document.body.innerHTML;
          const model = createBookmarksModel();
          const title = '<img src="/injected" onerror="window.injected=true">';
          const url = { id: 1, url: 'https://example.test', title, created_at: '', important: false, version: 1 };
          const group = (id, name, urls, children = []) => ({ id, name, urls, children, parent_id: null, domains: [], nsfw: false });
          model.replaceGroups([group(1, 'default', []), group(2, title, [url, url], [group(3, 'Deep', [url])])]);
          const root = document.createElement('div');
          const count = document.createElement('p');
          renderGroups(root, count, model.server.groups, model.ui, { iconSource: () => '', actions: {} });
          document.body.append(root);
          return { inert, count: count.textContent, rows: root.querySelectorAll('.url-item').length,
            empty: root.querySelector('.group-empty').textContent,
            protected: root.firstElementChild.querySelectorAll('.group-actions').length,
            title: root.querySelector('.url-text > a').textContent,
            markup: root.querySelector('.url-text > a').children.length,
            depths: [...root.querySelectorAll('.group')].map(node => node.dataset.depth) };
        }"""
    )
    assert result == {
        "inert": True,
        "count": "1 saved",
        "rows": 3,
        "empty": "No URLs yet.",
        "protected": 0,
        "title": '<img src="/injected" onerror="window.injected=true">',
        "markup": 0,
        "depths": ["1", "1", "2"],
    }


def test_empty_and_adjacent_children_preserve_native_order(static_page):
    result = static_page.evaluate(
        """async () => {
          const { h } = await import('/static/shared/dom.js');
          const first = h('span', {}, ['equal']);
          const second = h('span', {}, ['equal']);
          const parent = h('div', {}, ['same', 'same', first, second, 'e\u0301', 'é', '日本語🙂']);
          return {
            empty: h('input', { value: '', checked: false }, []).childNodes.length,
            children: [...parent.childNodes].map(node => [node.nodeType, node.textContent]),
            identity: parent.childNodes[2] === first && parent.childNodes[3] === second && first !== second,
          };
        }"""
    )
    assert result == {
        "empty": 0,
        "children": [
            [3, "same"],
            [3, "same"],
            [1, "equal"],
            [1, "equal"],
            [3, "e\u0301"],
            [3, "é"],
            [3, "日本語🙂"],
        ],
        "identity": True,
    }


def test_untrusted_text_is_inert_without_resource_requests(static_page):
    requests = []
    static_page.on("request", lambda request: requests.append(request.url))
    title = '<img src="/untrusted-image" onerror="window.injected = true"><script>window.injected = true</script>'
    result = static_page.evaluate(
        """async title => {
          const { h } = await import('/static/shared/dom.js');
          const element = h('a', { href: 'https://example.test', target: '_blank', rel: 'noopener noreferrer' }, [title]);
          document.body.append(element);
          await new Promise(resolve => requestAnimationFrame(resolve));
          return { text: element.textContent, elements: element.children.length, injected: window.injected ?? false };
        }""",
        title,
    )
    assert result == {"text": title, "elements": 0, "injected": False}
    assert all(url.endswith("/static/shared/dom.js") for url in requests)


def test_runtime_rejects_unsafe_properties_even_without_types(static_page):
    result = static_page.evaluate(
        """async () => {
          const { h } = await import('/static/shared/dom.js');
          return [
            { innerHTML: '<img>' }, { outerHTML: '<img>' }, { onclick: 'alert(1)' },
            { attributes: { onclick: 'alert(1)' } }, { unknown: true },
            { style: { cssText: 'color:red' } }, { data: { onclick: 'x' } },
            { aria: { mystery: 'x' } }, { href: 'https://example.test' },
          ].map(prop => {
            try { h('div', prop); return false; } catch { return true; }
          });
        }"""
    )
    assert result == [True] * 9


def test_live_url_title_keeps_markup_inert(app, page):
    base_url, _ = app
    title = (
        '<img src="/untrusted-title" onerror="window.injected = true"> e\u0301 日本語'
    )
    bookmark_helpers.seed_url("https://example.test", title=title)
    page.goto(base_url)
    link = page.locator(".url-text > a")
    expect(link).to_have_text(title)
    expect(link).to_have_attribute("href", "https://example.test")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    assert link.locator("*").count() == 0
    assert page.evaluate("window.injected ?? false") is False


def test_helper_properties_events_and_svg_namespace(static_page):
    result = static_page.evaluate(
        """async () => {
          const { h } = await import('/static/shared/dom.js');
          const { icon } = await import('/static/shared/icons.js');
          const calls = [];
          const button = h('button', {
            type: 'button', className: 'fold-toggle', hidden: false,
            aria: { label: 'Toggle', expanded: true, controls: 'content' },
            on: { click: event => { calls.push(event.type); event.stopPropagation(); } },
          }, [icon('star')]);
          const parent = h('section', {
            id: 'section', className: 'group', title: 'Title',
            data: { depth: '2', parentId: '1' }, style: { '--group-hue': '75' },
            on: { click: () => calls.push('parent') },
          }, [button, h('div', { id: 'content' }, ['text'])]);
          document.body.append(parent);
          button.click();
          const select = h('select', { value: '2' }, [
            h('option', { value: '1' }, ['One']), h('option', { value: '2' }, ['Two']),
          ]);
          const svg = button.firstElementChild;
          return {
            tag: parent.tagName, className: parent.className, title: parent.title,
            data: {...parent.dataset}, hue: parent.style.getPropertyValue('--group-hue'),
            children: [...parent.children].map(child => child.tagName),
            aria: [button.getAttribute('aria-label'), button.getAttribute('aria-expanded'), button.getAttribute('aria-controls')],
            calls, selected: select.value,
            svg: [svg.namespaceURI, svg.firstElementChild.namespaceURI, svg.getAttribute('aria-hidden'), svg.firstElementChild.getAttribute('href')],
          };
        }"""
    )
    assert result == {
        "tag": "SECTION",
        "className": "group",
        "title": "Title",
        "data": {"depth": "2", "parentId": "1"},
        "hue": "75",
        "children": ["BUTTON", "DIV"],
        "aria": ["Toggle", "true", "content"],
        "calls": ["click"],
        "selected": "2",
        "svg": [
            "http://www.w3.org/2000/svg",
            "http://www.w3.org/2000/svg",
            "true",
            "/static/icons.svg#star",
        ],
    }


def test_format_helpers_are_dom_independent(static_page):
    result = static_page.evaluate(
        """async () => {
          const format = await import('/static/shared/format.js');
          return [format.displayUrl('https://example.test/path/'),
            format.hostFor('https://example.test:8443/path'), format.hostFor('not a URL'),
            format.errorMessage(new Error('failure')), format.errorMessage(null),
            format.formatAdded('not a date'), Boolean(format.formatAdded('2026-01-01T00:00:00Z'))];
        }"""
    )
    assert result == [
        "example.test/path",
        "example.test:8443",
        "not a URL",
        "failure",
        "Unexpected error.",
        "",
        True,
    ]


def test_live_group_and_url_child_order_includes_drag_handle(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading", domains=["example.test"])
    bookmark_helpers.seed_url("https://example.test", title="A title")
    page.goto(base_url)
    group = page.locator(".group").filter(
        has=page.locator(".group-name", has_text="Reading")
    )
    expect(group.locator(".url-item")).to_have_count(1)
    assert group.evaluate(
        "element => [...element.children].map(child => child.className)"
    ) == ["group-header", "group-content"]
    assert group.locator(".group-header").evaluate(
        "element => [...element.children].map(child => child.className)"
    ) == ["fold-toggle", "group-name", "group-count", "group-domains", "group-actions"]
    assert group.locator(".url-item").evaluate(
        "element => [...element.children].map(child => child.className)"
    ) == ["url-main", "url-controls"]
    assert group.locator(".url-controls").evaluate(
        "element => [...element.children].map(child => child.className)"
    ) == [
        "url-drag-handle",
        "important-toggle",
        "url-action edit-url-button",
        "url-action refresh-metadata-button",
        "move-select",
        "delete-button",
    ]
    assert group.locator(".site-icon").evaluate(
        "element => [...element.children].map(child => child.className)"
    ) == ["site-icon-placeholder", "site-icon-image"]
    assert group.locator(".url-text").evaluate(
        "element => [...element.children].map(child => child.tagName)"
    ) == ["A", "SPAN"]
