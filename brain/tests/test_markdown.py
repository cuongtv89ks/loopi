from loopi_brain.web.markdown import render_markdown


def test_headings_lists_paragraphs_and_bold():
    html = render_markdown("## Diễn biến chính\n- Sáng **đọc sách**\n- Trưa nấu ăn\n\nMột đoạn\nhai dòng.")
    assert html == (
        "<h3>Diễn biến chính</h3>\n<ul><li>Sáng <strong>đọc sách</strong></li><li>Trưa nấu ăn</li></ul>\n"
        "<p>Một đoạn hai dòng.</p>"
    )


def test_model_text_cannot_inject_markup():
    html = render_markdown('- <script>alert(1)</script> [x](javascript:alert(1)) <img src=x onerror=y>\n# <b>t</b>')
    assert "<script>" not in html and "<img" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "<h2>&lt;b&gt;t&lt;/b&gt;</h2>" in html
    assert "href" not in html  # links are text


def test_a_list_after_a_paragraph_and_back():
    assert render_markdown("Mở đầu\n- một\nKết") == "<p>Mở đầu</p>\n<ul><li>một</li></ul>\n<p>Kết</p>"
    assert render_markdown("") == ""
