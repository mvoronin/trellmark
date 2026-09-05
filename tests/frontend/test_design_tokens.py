"""Checks the design system's two measurable promises: AA contrast in both
themes, and no horizontal overflow on a narrow phone."""

import pytest

from tests.bookmarks import helpers as bookmark_helpers

# Reads every distinct text style on the page, resolves its color against the
# nearest painted ancestor background, and returns the WCAG ratio.
#
# Computed colors come back as oklch() and canvas keeps them in that form, so
# the conversion goes through a painted pixel: that is the engine's own
# oklch -> sRGB math, gamut mapping included, rather than a second
# implementation of it living in this file.
CONTRAST_JS = """
() => {
  const cv = document.createElement("canvas").getContext("2d", {
    willReadFrequently: true,
  });
  const srgb = (css) => {
    cv.clearRect(0, 0, 1, 1);
    cv.fillStyle = css;
    cv.fillRect(0, 0, 1, 1);
    const d = cv.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  };
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const lum = (css) => {
    const [r, g, b] = srgb(css);
    return 0.2126 * lin(r / 255) + 0.7152 * lin(g / 255) + 0.0722 * lin(b / 255);
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((m, n) => n - m);
    return (hi + 0.05) / (lo + 0.05);
  };
  // Walk up for the nearest painted background, the way the eye does.
  const bgOf = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      if (bg && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(bg)) return bg;
    }
    return getComputedStyle(document.body).backgroundColor;
  };
  const rows = [];
  const seen = new Set();
  for (const el of document.querySelectorAll("body *")) {
    if (!el.textContent.trim() || el.children.length) continue;
    const key = el.className || el.tagName;
    if (seen.has(key)) continue;
    seen.add(key);
    const cs = getComputedStyle(el);
    rows.push({
      what: key,
      px: parseFloat(cs.fontSize),
      weight: Number(cs.fontWeight),
      ratio: ratio(cs.color, bgOf(el)),
    });
  }
  return {
    rows,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  };
}
"""


def _seed():
    """A page with every styled surface on it: a filled band, an important row,
    an NSFW badge, an empty group, and a long unbreakable URL."""
    reading = bookmark_helpers.seed_group("Reading", domains=["arxiv.org"])
    bookmark_helpers.seed_group("Archive")
    adult = bookmark_helpers.seed_group("Adult", nsfw=True)

    starred = bookmark_helpers.seed_url(
        "https://example.com/a-fairly-long-article-title"
    )
    bookmark_helpers.seed_title(starred["id"], "Constructivism and the Grid")
    bookmark_helpers.seed_important(starred["id"], True)

    filed = bookmark_helpers.seed_url("https://arxiv.org/abs/2401.00001")
    bookmark_helpers.seed_membership(filed["id"], reading["id"])

    hidden = bookmark_helpers.seed_url("https://adult.example/gallery")
    bookmark_helpers.seed_membership(hidden["id"], adult["id"])

    bookmark_helpers.seed_url("https://very-long-domain-name.example.net/deep/path/x")


def _measure(page, base_url, theme, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    page.evaluate(
        "t => { localStorage.setItem('theme', t);"
        "document.documentElement.dataset.theme = t; }",
        theme,
    )
    page.reload()
    # "All" reveals the NSFW group, whose badge is the tightest pairing.
    page.get_by_role("button", name="All", exact=True).click()
    return page.evaluate(CONTRAST_JS)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 1440])
def test_frontend_text_meets_wcag_aa(app, page, theme, width):
    base_url, _ = app
    _seed()

    result = _measure(page, base_url, theme, width)

    failures = []
    for row in result["rows"]:
        large = row["px"] >= 24 or (row["px"] >= 18.66 and row["weight"] >= 700)
        floor = 3.0 if large else 4.5
        if row["ratio"] < floor:
            failures.append(
                f"{row['what']}: {row['ratio']:.2f}:1 needs {floor}:1 "
                f"({row['px']:.0f}px/{row['weight']})"
            )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_frontend_has_no_horizontal_overflow_on_a_phone(app, page, theme):
    base_url, _ = app
    _seed()

    result = _measure(page, base_url, theme, 360)

    assert result["scrollWidth"] <= result["clientWidth"]


# Keep the original application sampler and expectations intact. The overview
# samples each visible text node separately, including every state and modal,
# and accounts for ancestor opacity instead of deduplicating by class name.
DESIGN_CONTRAST_JS = (
    CONTRAST_JS.replace(
        "const rows = [];",
        """const opacityRatio = (el, foreground, background) => {
          let fg = srgb(foreground), bg = srgb(background);
          for (let node = el; node; node = node.parentElement) {
            const opacity = Number(getComputedStyle(node).opacity);
            if (opacity === 1) continue;
            const behind = srgb(bgOf(node.parentElement));
            fg = fg.map((value, i) => value * opacity + behind[i] * (1 - opacity));
            bg = bg.map((value, i) => value * opacity + behind[i] * (1 - opacity));
          }
          return ratio(`rgb(${fg.join(' ')})`, `rgb(${bg.join(' ')})`);
        };
        const rows = [];""",
    )
    .replace(
        'document.querySelectorAll("body *")',
        'document.querySelectorAll(document.querySelector("dialog[open]") ? "dialog[open] *" : "body *")',
    )
    .replace(
        "if (!el.textContent.trim() || el.children.length) continue;",
        """if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
        if (![...el.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())) continue;""",
    )
    .replace(
        "const key = el.className || el.tagName;",
        """const state = el.closest('[data-design-state]')?.dataset.designState || el.closest('dialog')?.id || 'frame';
        const key = `${state}/${el.id || el.className || el.tagName}/${rows.length}`;""",
    )
    .replace(
        "ratio: ratio(cs.color, bgOf(el)),",
        "ratio: opacityRatio(el, cs.color, bgOf(el)),",
    )
    .replace(
        "clientWidth: document.documentElement.clientWidth,",
        """clientWidth: document.documentElement.clientWidth,
        overflows: [...document.querySelectorAll('dialog[open], .design-section, .group, .url-item, .group-header')]
          .filter(el => el.getClientRects().length && el.scrollWidth > el.clientWidth + 1)
          .map(el => ({state: el.closest('[data-design-state]')?.dataset.designState || el.id,
            className: el.className, scroll: el.scrollWidth, client: el.clientWidth})),""",
    )
)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 1440])
@pytest.mark.parametrize(
    "dialog_id",
    [
        None,
        "url-edit-dialog",
        "group-edit-dialog",
        "confirm-dialog",
        "group-delete-dialog",
    ],
)
def test_design_states_and_dialogs_have_contrast_and_no_overflow(
    static_page, static_web_server, theme, width, dialog_id
):
    static_page.set_viewport_size({"width": width, "height": 900})
    static_page.goto(f"{static_web_server}/design.html")
    static_page.wait_for_selector("#groups .url-item")
    static_page.locator(f'#app-view [data-theme-value="{theme}"]').click()
    if dialog_id:
        static_page.locator(f'[data-design-dialog="{dialog_id}"]').click()
    result = static_page.evaluate(DESIGN_CONTRAST_JS)
    assert result["rows"], "No component text was sampled"
    failures = []
    for row in result["rows"]:
        large = row["px"] >= 24 or (row["px"] >= 18.66 and row["weight"] >= 700)
        floor = 3.0 if large else 4.5
        if row["ratio"] < floor:
            failures.append(f"{row['what']}: {row['ratio']:.2f}:1 needs {floor}:1")
    assert not failures, "\n".join(failures)
    assert result["scrollWidth"] <= result["clientWidth"]
    assert not result["overflows"], result["overflows"]
