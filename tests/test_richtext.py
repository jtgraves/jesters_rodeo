from __future__ import annotations

from markupsafe import Markup

from app.richtext import render_richtext


def test_empty_and_none_render_empty():
    assert render_richtext(None) == Markup("")
    assert render_richtext("") == Markup("")
    assert render_richtext("   ") == Markup("")


def test_plain_text_becomes_one_paragraph():
    assert render_richtext("Just some text.") == Markup("<p>Just some text.</p>")


def test_single_newline_becomes_br_double_newline_starts_a_paragraph():
    out = render_richtext("Line one.\nLine two.\n\nLine three.")
    assert out == Markup("<p>Line one.<br>Line two.</p><p>Line three.</p>")


def test_bold_and_italic():
    assert render_richtext("**bold** and *italic*") == \
        Markup("<p><strong>bold</strong> and <em>italic</em></p>")


def test_headings_start_at_h3_and_nest_by_hash_count():
    out = render_richtext("# Title\n\n## Subtitle\n\n### Smaller")
    assert out == Markup("<h3>Title</h3><h4>Subtitle</h4><h5>Smaller</h5>")


def test_heading_immediately_followed_by_a_paragraph_no_blank_line():
    out = render_richtext("# Schedule\n7pm - doors open")
    assert out == Markup("<h3>Schedule</h3><p>7pm - doors open</p>")


def test_bullet_list():
    out = render_richtext("- one\n- two\n- three")
    assert out == Markup("<ul><li>one</li><li>two</li><li>three</li></ul>")


def test_bullet_list_can_use_a_star_marker_too():
    assert render_richtext("* one\n* two") == Markup("<ul><li>one</li><li>two</li></ul>")


def test_list_then_paragraph_without_blank_line():
    out = render_richtext("- one\n- two\nAfter the list.")
    assert out == Markup("<ul><li>one</li><li>two</li></ul><p>After the list.</p>")


def test_http_link_is_rendered():
    out = render_richtext("See [our site](https://example.com/x) for more.")
    assert out == Markup(
        '<p>See <a href="https://example.com/x" target="_blank" rel="noopener">'
        "our site</a> for more.</p>"
    )


def test_non_http_link_scheme_is_left_as_plain_text():
    # javascript:/data: etc. never match the http(s)-only pattern, so the
    # literal, already-escaped bracket text passes through unrendered.
    out = render_richtext("[click me](javascript:alert(1))")
    assert "<a " not in out
    assert "javascript:alert(1)" in out


def test_raw_html_in_the_input_is_neutralized():
    out = render_richtext('<script>alert(1)</script> and <img src=x onerror=alert(1)>')
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;" in out


def test_html_special_characters_are_escaped_before_formatting_is_applied():
    out = render_richtext("Tom & Jerry's \"great\" day")
    assert "&amp;" in out
    assert "&#39;" in out
    assert "&#34;" in out or "&quot;" in out


def test_link_url_itself_is_escaped_and_cannot_break_out_of_the_attribute():
    out = render_richtext('[x](https://example.com/"><script>alert(1)</script>)')
    assert "<script>" not in out
    assert "&quot;" in out or "&#34;" in out


def test_result_is_markup_and_not_double_escaped_by_jinja():
    from app.templating import templates  # registers the "richtext" filter on import
    rendered = templates.env.from_string("{{ value | richtext }}").render(value="**bold**")
    assert rendered == "<p><strong>bold</strong></p>"


def test_returns_a_markup_instance():
    assert isinstance(render_richtext("hi"), Markup)
