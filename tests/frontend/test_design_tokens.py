"""Checks the design system's two measurable promises: AA contrast in both
themes, and no horizontal overflow on a narrow phone."""

import pytest

import trellmark

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
    reading = trellmark.add_group("Reading", domains=["arxiv.org"])
    trellmark.add_group("Archive")
    adult = trellmark.add_group("Adult", nsfw=True)

    starred = trellmark.add_url("https://example.com/a-fairly-long-article-title")
    trellmark.update_url_title(starred["id"], "Constructivism and the Grid")
    trellmark.set_url_important(starred["id"], True)

    filed = trellmark.add_url("https://arxiv.org/abs/2401.00001")
    trellmark.move_url_to_group(filed["id"], reading["id"])

    hidden = trellmark.add_url("https://adult.example/gallery")
    trellmark.move_url_to_group(hidden["id"], adult["id"])

    trellmark.add_url("https://very-long-domain-name.example.net/deep/path/x")


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
