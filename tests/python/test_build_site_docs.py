"""The website documentation generator renders every document, and its output is sound.

Every page of docs/ is rendered; every link to another page or anchor in the output
resolves; no page contains a script, an inline event handler or a javascript: link;
and the Markdown reader handles the constructs the documents use."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_site_docs as site  # noqa: E402

ATTRIBUTE = re.compile(r"""\b(?:href|src)="([^"]*)\"""")
ID = re.compile(r"""\bid="([^"]+)\"""")


def render(markdown):
    """Render a Markdown string as a page body, with no files involved."""
    page = site.Page("README", ROOT / "docs" / "README.md")
    renderer = site.Renderer({"README": page}, "main", set())
    renderer.page = page
    renderer.slugs = {}
    return renderer.blocks(markdown.splitlines()), renderer.errors


class RendersEveryDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        cls.out = Path(cls.directory.name)
        cls.pages = site.build(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_every_markdown_document_has_a_page(self):
        documents = {p.relative_to(ROOT / "docs").with_suffix("").as_posix() for p in (ROOT / "docs").rglob("*.md")}
        self.assertEqual(documents, set(self.pages))
        for page in self.pages.values():
            self.assertTrue((self.out / page.out).is_file(), page.out)

    def target(self, url):
        path = url.removeprefix(site.URL_BASE).strip("/")
        if not path:
            return self.out / "index.html"
        for candidate in (self.out / (path + ".html"), self.out / path / "index.html", self.out / path):
            if candidate.is_file():
                return candidate
        return None

    def test_internal_links_and_anchors_resolve(self):
        ids = {}
        for html_file in self.out.rglob("*.html"):
            ids[html_file] = set(ID.findall(html_file.read_text(encoding="utf-8")))
        checked = 0
        for html_file in self.out.rglob("*.html"):
            text = html_file.read_text(encoding="utf-8")
            for url in ATTRIBUTE.findall(text):
                if re.match(r"[a-z][a-z0-9+.-]*:", url) and not url.startswith("javascript"):
                    continue  # external
                with self.subTest(page=html_file.relative_to(self.out).as_posix(), link=url):
                    path, _, fragment = url.partition("#")
                    if path.startswith("/") and not path.startswith(site.URL_BASE):
                        self.assertTrue(path in ("/", "/agents", "/changelog", "/imprint", "/privacy", "/disclaimer", "/favicon.svg",
                                                 "/favicon.ico", "/assets/site.css", "/skill", "/assets/logo/tomacco-color.svg"), path)
                        continue
                    target = self.target(path) if path else html_file
                    self.assertIsNotNone(target, "no generated page")
                    if fragment:
                        self.assertIn(fragment, ids[target], "no such anchor")
                    checked += 1
        self.assertGreater(checked, 500)

    def test_no_script_anywhere(self):
        for html_file in self.out.rglob("*.html"):
            text = html_file.read_text(encoding="utf-8")
            with self.subTest(page=html_file.name):
                self.assertNotRegex(text, r"(?i)<script|javascript:|\son[a-z]+=|<iframe|<object|<embed|<style")

    def test_pages_have_a_title_and_one_h1(self):
        for page in self.pages.values():
            text = (self.out / page.out).read_text(encoding="utf-8")
            with self.subTest(page=page.key):
                self.assertEqual(text.count("<h1 "), 1)
                self.assertRegex(text, r"<title>[^<]+</title>")
                self.assertIn(f'<link rel="canonical" href="{site.SITE}{page.url}">', text)

    def test_no_markdown_left_over_in_prose(self):
        for page in self.pages.values():
            body = re.sub(r"<pre>.*?</pre>", "", page.body, flags=re.S)
            body = re.sub(r"<code>.*?</code>", "", body, flags=re.S)
            with self.subTest(page=page.key):
                # **/ is a path glob in operation descriptions, not bold text.
                self.assertNotRegex(body, r"\*\*(?!/)\S|\]\([^)]*\.md")

    def test_images_are_copied(self):
        self.assertTrue((self.out / "images" / "tutorial-android-counter.png").is_file())


class MarkdownReader(unittest.TestCase):
    def test_escapes_html(self):
        body, _ = render("A <script>alert(1)</script> & <path> here, `<b>` too.")
        self.assertNotIn("<script", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;path&gt;", body)
        self.assertIn("<code>&lt;b&gt;</code>", body)

    def test_rejects_javascript_links(self):
        _, errors = render("[x](javascript:alert(1))")
        self.assertTrue(errors)

    def test_headings_get_github_ids(self):
        body, _ = render("## Set up `x`\n\n## Set up `x`\n")
        self.assertIn('id="set-up-x"', body)
        self.assertIn('id="set-up-x-1"', body)

    def test_inline_forms(self):
        body, _ = render("**bold** and *em* and snake_case_name and `a_b` and [link](https://example.com).")
        self.assertIn("<strong>bold</strong>", body)
        self.assertIn("<em>em</em>", body)
        self.assertIn("snake_case_name", body)
        self.assertIn('<a href="https://example.com">link</a>', body)

    def test_emphasis_and_links_span_lines(self):
        body, _ = render("**one\ntwo** and [a\nb](https://example.com)")
        self.assertIn("<strong>one\ntwo</strong>", body)
        self.assertIn('<a href="https://example.com">a\nb</a>', body)

    def test_fenced_code_is_literal(self):
        body, _ = render("```sh\necho \"<x>\" | **y**\n```")
        self.assertIn('<pre><code class="language-sh">echo "&lt;x&gt;" | **y**\n</code></pre>', body)

    def test_table(self):
        body, _ = render("| A | B |\n| --- | :---: |\n| `x\\|y` | 2 |\n")
        self.assertIn("<th>A</th>", body)
        self.assertIn('<th style="text-align:center">B</th>', body)
        self.assertIn("<td><code>x|y</code></td>", body)

    def test_nested_and_ordered_lists(self):
        body, _ = render("1. one\n2. two\n   - inner\n   - inner two\n3. three\n")
        self.assertEqual(body.count("<ol>"), 1)
        self.assertEqual(body.count("<ul>"), 1)
        self.assertEqual(body.count("<li>"), 5)

    def test_list_item_with_code_and_continuation(self):
        body, _ = render("- first line\n  continued\n- with code:\n\n  ```sh\n  ls\n  ```\n")
        self.assertIn("first line\ncontinued", body)
        self.assertIn("<pre><code", body)
        self.assertEqual(body.count("<li>"), 2)

    def test_blockquote_and_details(self):
        body, _ = render("> quoted *text*\n\n<details>\n<summary>More</summary>\n\nInside.\n\n</details>\n")
        self.assertIn("<blockquote>", body)
        self.assertIn("<summary>More</summary>", body)
        self.assertIn("<p>Inside.</p>", body)

    def test_html_comment_is_dropped(self):
        body, _ = render("<!-- Generated. Do not edit. -->\n\nText")
        self.assertEqual(body, "<p>Text</p>")

    def test_missing_link_target_is_an_error(self):
        _, errors = render("[x](no-such-file.md)")
        self.assertTrue(errors)

    def test_file_outside_docs_links_to_github(self):
        body, errors = render("[license](../LICENSE)")
        self.assertFalse(errors)
        self.assertIn(f"{site.REPO}/blob/main/LICENSE", body)


if __name__ == "__main__":
    unittest.main()
