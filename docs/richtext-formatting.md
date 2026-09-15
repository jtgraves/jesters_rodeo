# Formatted text fields

Event/charity/beneficiary descriptions, clown bios, and FAQ answers accept a
small formatting subset, rendered by `app/richtext.py`:

| Type this | Get this |
| --- | --- |
| `# Heading` | `<h3>` |
| `## Subheading` | `<h4>` |
| `### Smaller heading` | `<h5>` |
| `**bold**` | `<strong>` |
| `*italic*` | `<em>` |
| `- item` (one per line) | `<ul><li>` |
| `[label](https://example.com)` | a link — `http(s)://` only |
| a blank line | starts a new paragraph |
| a single line break | `<br>` within the current paragraph |

Every admin form editing one of these fields shows a small toolbar
(`app/templates/admin/_richtext_toolbar.html`) that inserts this syntax
around the current selection, plus a one-line hint below the textarea — no
syntax to memorize.

## Why this and not a Markdown library

This is deliberately **not** a general Markdown implementation. See
`app/richtext.py`'s module docstring for the exact reasoning, but in short:
the app has no build step and has consistently avoided new dependencies this
project (CSV import stayed stdlib-only, image resizing is a hand-written
`<canvas>` script rather than a library). A real Markdown parser plus an HTML
sanitizer would be two new PyPI dependencies for a feature that only needs
headings, bold, italic, lists, and links.

**Safety comes from order, not an allow-list applied afterward:** the entire
field value is HTML-escaped before any tag is introduced, so nothing typed or
pasted into the field can ever reach the page as a tag — only the specific
tags this module builds itself, over text that's already inert. This is a
narrower guarantee than a real sanitizer's (it can't, say, allow a `<table>`
someone wants later) but it's small enough to read and test completely,
which matters more here than flexibility — this is the one piece of code
that turns admin-authored text into live HTML on a public page.

## Adding this to a new field

1. Store the field as a plain string, same as today — no schema/model change.
2. Edit form: add `class="richtext-input"` to the `<textarea>`, and
   `{% include "admin/_richtext_toolbar.html" %}` once per page (it attaches
   to every `.richtext-input` textarea on the page, so one include covers
   several fields/forms).
3. Display template: `{{ field | richtext }}`, wrapped in a block element
   (`<div>`, not `<p>` — the renderer emits its own `<p>`/heading/`<ul>`
   tags, which can't nest inside a `<p>`) with class `richtext` for the
   shared spacing rules in `app/static/style.css`.

## If richer formatting is needed later

Swap `app/richtext.py`'s renderer for a real Markdown library (e.g.
`markdown` or `mistune`) plus an HTML sanitizer (e.g. `nh3`) run on its
output. The storage format (plain text with embedded lightweight-markdown
syntax) and the call sites (`| richtext` in templates, `richtext-input` +
the toolbar partial in forms) don't need to change — only the function
`render_richtext` does.
