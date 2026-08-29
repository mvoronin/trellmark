# Vendored fonts

Trellmark must render without a third-party font request, so the two typefaces
are served from `web/static/` alongside `icons.svg` rather than fetched from a CDN.
Each family is split by `unicode-range` in `styles.css`, so a browser only
downloads the scripts it actually paints.

| Family | Files | Role |
| --- | --- | --- |
| PT Sans Narrow | `ptsansnarrow-*.woff2` (400, 700) | Interface voice: bands, labels, buttons |
| Golos Text | `golostext-*.woff2` (variable, 400–700) | Content: link titles, metadata, fields, dialogs |

Both families use the SIL Open Font License 1.1. These subsets came from the
Google Fonts distribution. The complete licenses and original copyright
notices are included as `PTSansNarrow-OFL.txt` and `GolosText-OFL.txt`.
