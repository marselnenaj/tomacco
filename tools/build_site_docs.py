#!/usr/bin/env python3
"""Render the Markdown documentation to static HTML pages for the project website.

    python3 tools/build_site_docs.py OUTPUT_DIR [--sitemap SITE/sitemap.xml]

OUTPUT_DIR is the site's docs/ directory, served at /docs. The pages use the site's
stylesheet (/assets/site.css), contain no script, and link to each other with the same
extensionless URLs the site uses for /agent and /agents: /docs/faq is docs/faq.html. A
README.md becomes the index.html of its directory and is linked with a trailing slash.
Links to files that are not documentation point at GitHub.

Only the standard library is used. The Markdown reader covers what the documents use:
headings, paragraphs, lists, fenced code, tables, quotes, emphasis, code spans, links,
images, and the HTML elements <details>, <summary>, <br>, <b>, <sub>, <kbd>, <a>, <img>.
Anything else is escaped, so no raw HTML reaches the output.
"""
import argparse
import datetime
import html
import posixpath
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://tomacco.dev"
REPO = "https://github.com/marselnenaj/tomacco"
URL_BASE = "/docs"

# Navigation: group title and the pages in it, as paths below docs/ (without .md).
GROUPS = [
    ("Start", ["getting-started", "tutorial-android", "tutorial-virtual-iphone", "agent-setup", "recipes"]),
    ("Agents", ["agents", "agent-patterns", "container"]),
    ("Use Tomacco", ["desktop", "android", "iphone", "linux-hosts"]),
    ("Virtual iOS device", ["virtual-ios/README", "virtual-ios/setup", "virtual-ios/usage",
                            "virtual-ios/troubleshooting", "virtual-ios/how-it-works"]),
    ("Reference", ["reference/README", "reference/errors"]),
    ("Help", ["troubleshooting", "faq"]),
    ("Project", ["architecture", "building", "research-engines", "legal", "privacy", "RELEASE_NOTES"]),
]
SHORT_TITLES = {
    "README": "Overview",
    "virtual-ios/README": "About",
    "virtual-ios/setup": "Set up",
    "virtual-ios/usage": "Use",
    "virtual-ios/troubleshooting": "Troubleshooting",
    "virtual-ios/how-it-works": "How it works",
    "reference/README": "Operations",
    "tutorial-android": "First Android app",
    "tutorial-virtual-iphone": "First SwiftUI app",
    "agent-patterns": "Patterns for agent authors",
    "RELEASE_NOTES": "Release notes",
}

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
HRULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( +|$)(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"([^\"]*)\")?\s*\)")
AUTOLINK = re.compile(r"<(https?://[^\s<>]+)>")
CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
INLINE_TAG = re.compile(r"</?(?:br|b|sub|sup|kbd|i|em|strong)\s*/?>", re.I)
OPEN_A = re.compile(r"<a\s+href=\"([^\"]*)\"\s*>", re.I)
IMG_TAG = re.compile(r"<img\b([^>]*?)/?>", re.I)
ATTR = re.compile(r"([a-z-]+)=\"([^\"]*)\"", re.I)


class BuildError(Exception):
    pass


def slugify(text):
    """The id GitHub gives a heading: the same rule tests/python/test_docs_links.py checks."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def plain(text):
    """Heading or title text without Markdown markers."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)
    return re.sub(r"[`*]|(?<!\w)_|_(?!\w)", "", text).strip()


class Page:
    def __init__(self, key, path):
        self.key = key  # "faq", "virtual-ios/README"
        self.path = path  # Path of the .md
        self.source = path.relative_to(ROOT).as_posix()
        self.is_index = key.endswith("README") and key != "RELEASE_NOTES"
        directory = posixpath.dirname(key)
        name = posixpath.basename(key)
        if name == "README":
            self.out = posixpath.join(directory, "index.html")
            self.url = f"{URL_BASE}/{directory}/" if directory else f"{URL_BASE}/"
        else:
            self.out = posixpath.join(directory, name + ".html")
            self.url = f"{URL_BASE}/{key}"
        self.title = key
        self.headings = []  # (level, id, plain text)
        self.ids = set()
        self.body = ""
        self.description = ""
        self.links = []  # (target page key or None, fragment) to verify


class Renderer:
    def __init__(self, pages, ref, images):
        self.pages = pages  # key -> Page
        self.by_path = {p.path.resolve(): p for p in pages.values()}
        self.ref = ref
        self.images = images  # set of Paths to copy
        self.errors = []
        self.page = None
        self.slugs = {}

    def error(self, message):
        self.errors.append(f"{self.page.source}: {message}")

    # ---- links -------------------------------------------------------------------

    def link(self, href, image=False):
        """The URL for a link or image target found in the current document."""
        if EXTERNAL.match(href):
            if not re.match(r"(https?|mailto):", href, re.I):
                self.error(f"link scheme not allowed: {href}")
                return "#"
            return href
        path, _, fragment = href.partition("#")
        if not path:
            self.page.links.append((self.page.key, fragment))
            return "#" + fragment
        target = (self.page.path.parent / path).resolve()
        if image:
            try:
                target.relative_to(ROOT / "docs")
            except ValueError:
                self.error(f"image outside docs/: {href}")
                return "#"
            if not target.is_file():
                self.error(f"missing image: {href}")
                return "#"
            self.images.add(target)
            return f"{URL_BASE}/" + target.relative_to(ROOT / "docs").as_posix()
        page = self.by_path.get(target)
        if page:
            self.page.links.append((page.key, fragment))
            return page.url + ("#" + fragment if fragment else "")
        try:
            relative = target.relative_to(ROOT).as_posix()
        except ValueError:
            self.error(f"link leaves the repository: {href}")
            return "#"
        if not target.exists():
            self.error(f"link target does not exist: {href}")
            return "#"
        kind = "tree" if target.is_dir() else "blob"
        return f"{REPO}/{kind}/{self.ref}/{relative}" + ("#" + fragment if fragment else "")

    # ---- inline ------------------------------------------------------------------

    def inline(self, text):
        stash = []

        def keep(markup):
            stash.append(markup)
            return f"\x00{len(stash) - 1}\x00"

        text = text.replace("\x00", "")
        text = CODE_SPAN.sub(lambda m: keep("<code>" + html.escape(self.code_text(m.group(2)), quote=False) + "</code>"), text)
        text = ESCAPED.sub(lambda m: keep(html.escape(m.group(1), quote=False)), text)
        text = AUTOLINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1), quote=False)}</a>'), text)

        def img(m):
            attrs = dict(ATTR.findall(m.group(1)))
            if "src" not in attrs:
                self.error("<img> without src")
                return ""
            width = re.fullmatch(r"\d{1,4}", attrs.get("width", ""))
            return keep(f'<img src="{html.escape(self.link(attrs["src"], True))}" alt="{html.escape(attrs.get("alt", ""))}" loading="lazy"'
                        + (f' width="{width.group(0)}"' if width else "") + ">")
        text = IMG_TAG.sub(img, text)
        text = OPEN_A.sub(lambda m: keep(f'<a href="{html.escape(self.link(m.group(1)))}">'), text)
        text = re.sub(r"</a\s*>", lambda m: keep("</a>"), text, flags=re.I)
        text = INLINE_TAG.sub(lambda m: keep(re.sub(r"\s|/(?=>)", "", m.group(0).lower())), text)
        text = self.spans(text, keep)
        text = html.escape(text, quote=False)
        text = self.emphasis(text)
        # Restore, innermost first: stashed markup may hold placeholders of its own.
        for _ in range(4):
            text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
        return text

    @staticmethod
    def code_text(raw):
        raw = raw.replace("\n", " ")
        if raw.strip() and raw.startswith(" ") and raw.endswith(" "):
            raw = raw[1:-1]
        return raw

    def spans(self, text, keep):
        def link(m):
            bang, label, href, title = m.groups()
            url = html.escape(self.link(href, bool(bang)))
            tail = f' title="{html.escape(title)}"' if title else ""
            if bang:
                return keep(f'<img src="{url}" alt="{html.escape(plain(label))}" loading="lazy"{tail}>')
            inner = self.emphasis(html.escape(self.spans(label, keep), quote=False))
            return keep(f'<a href="{url}"{tail}>{inner}</a>')
        return LINK.sub(link, text)

    @staticmethod
    def emphasis(text):
        text = re.sub(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<em>\1</em>", text, flags=re.S)
        return re.sub(r"(?<!~)~~(?=\S)(.+?)(?<=\S)~~(?!~)", r"<del>\1</del>", text, flags=re.S)

    # ---- blocks ------------------------------------------------------------------

    def heading_id(self, text):
        slug = slugify(text)
        count = self.slugs.get(slug, 0)
        self.slugs[slug] = count + 1
        return slug if count == 0 else f"{slug}-{count}"

    def blocks(self, lines, tight=False):
        out = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = FENCE.match(line)
            if fence:
                marker, language = fence.group(1), fence.group(2)
                indent = len(line) - len(line.lstrip(" "))
                body = []
                i += 1
                while i < n:
                    close = FENCE.match(lines[i])
                    if close and close.group(1)[0] == marker[0] and len(close.group(1)) >= len(marker) and lines[i].strip().strip(marker[0]) == "":
                        break
                    body.append(re.sub(rf"^ {{0,{indent}}}", "", lines[i]))
                    i += 1
                i += 1
                cls = f' class="language-{html.escape(language)}"' if language else ""
                out.append(f"<pre><code{cls}>{html.escape(chr(10).join(body), quote=False)}\n</code></pre>")
                continue
            if line.lstrip().startswith("<!--"):
                while i < n and "-->" not in lines[i]:
                    i += 1
                i += 1
                continue
            heading = HEADING.match(line)
            if heading:
                level = len(heading.group(1))
                raw = heading.group(2)
                ident = self.heading_id(raw)
                self.page.headings.append((level, ident, plain(raw)))
                self.page.ids.add(ident)
                out.append(f'<h{level} id="{ident}">{self.inline(raw)}<a class="anchor" href="#{ident}" aria-label="Link to this section">#</a></h{level}>')
                i += 1
                continue
            if HRULE.match(line):
                out.append("<hr>")
                i += 1
                continue
            tag = re.fullmatch(r"\s*<(/?)(details)>\s*", line, re.I)
            if tag:
                out.append(f"<{tag.group(1)}details>")
                i += 1
                continue
            summary = re.fullmatch(r"\s*<summary>(.*)</summary>\s*", line, re.I)
            if summary:
                out.append(f"<summary>{self.inline(summary.group(1))}</summary>")
                i += 1
                continue
            if line.startswith(">"):
                quote = []
                while i < n and lines[i].strip() and (lines[i].startswith(">") or quote and not self.starts_block(lines, i)):
                    quote.append(re.sub(r"^> ?", "", lines[i]))
                    i += 1
                out.append("<blockquote>\n" + self.blocks(quote) + "\n</blockquote>")
                continue
            if "|" in line and i + 1 < n and TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
                i = self.table(lines, i, out)
                continue
            if ITEM.match(line) and not HRULE.match(line):
                i = self.list(lines, i, out)
                continue
            para = [line.strip()]
            i += 1
            while i < n and lines[i].strip() and not self.starts_block(lines, i):
                para.append(lines[i].strip())
                i += 1
            text = self.inline_lines(para)
            out.append(text if tight else f"<p>{text}</p>")
        return "\n".join(out)

    def inline_lines(self, para):
        """One paragraph, lines joined so a link or emphasis may span them; a hard break becomes <br>."""
        joined = []
        for k, line in enumerate(para):
            hard = k < len(para) - 1 and (line.endswith("  ") or line.endswith("\\"))
            joined.append(line.rstrip("\\ ") + ("<br>" if hard else "") if hard else line.rstrip())
        return self.inline("\n".join(joined))

    def starts_block(self, lines, i):
        line = lines[i]
        if FENCE.match(line) or HEADING.match(line) or HRULE.match(line) or line.startswith(">"):
            return True
        if re.fullmatch(r"\s*</?details>\s*", line, re.I) or re.fullmatch(r"\s*<summary>.*</summary>\s*", line, re.I):
            return True
        item = ITEM.match(line)
        if item and (item.group(2) in "-*+" or item.group(2) in ("1.", "1)")) and item.group(4):
            return True
        return "|" in line and i + 1 < len(lines) and bool(TABLE_SEP.match(lines[i + 1])) and "-" in lines[i + 1]

    @staticmethod
    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|") and not line.endswith("\\|"):
            line = line[:-1]
        return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]

    def table(self, lines, i, out):
        head = self.cells(lines[i])
        align = []
        for cell in self.cells(lines[i + 1]):
            align.append("center" if cell.startswith(":") and cell.endswith(":") else "right" if cell.endswith(":") else "")
        i += 2
        rows = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i] and not self.starts_block(lines, i):
            rows.append(self.cells(lines[i]))
            i += 1

        def cell(tag, text, k):
            style = f' style="text-align:{align[k]}"' if k < len(align) and align[k] else ""
            return f"<{tag}{style}>{self.inline(text)}</{tag}>"
        html_rows = ["<tr>" + "".join(cell("th", c, k) for k, c in enumerate(head)) + "</tr>"]
        for row in rows:
            row = (row + [""] * len(head))[:len(head)]
            html_rows.append("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(row)) + "</tr>")
        out.append('<div class="table-wrap"><table>\n<thead>' + html_rows[0] + "</thead>\n<tbody>\n"
                   + "\n".join(html_rows[1:]) + "\n</tbody>\n</table></div>")
        return i

    def list(self, lines, i, out):
        first = ITEM.match(lines[i])
        base = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items = []
        n = len(lines)
        while i < n:
            m = ITEM.match(lines[i])
            if not m or len(m.group(1)) != base or (m.group(2)[0].isdigit() != ordered) or HRULE.match(lines[i]):
                break
            offset = len(m.group(1)) + len(m.group(2)) + max(len(m.group(3)), 1)
            body = [m.group(4)]
            i += 1
            blank = False
            while i < n:
                line = lines[i]
                if not line.strip():
                    blank = True
                    body.append("")
                    i += 1
                    continue
                indent = len(line) - len(line.lstrip(" "))
                if indent > base:
                    body.append(line[min(indent, offset):] if indent >= offset else line.lstrip(" "))
                    blank = False
                    i += 1
                elif not blank and indent == base and not ITEM.match(line) and not self.starts_block(lines, i):
                    body.append(line.strip())
                    i += 1
                elif not blank and indent < base and indent > 0 and not ITEM.match(line) and not self.starts_block(lines, i):
                    body.append(line.strip())
                    i += 1
                else:
                    break
            while body and not body[-1].strip():
                body.pop()
            items.append(body)
            # A blank line then another item of this list continues it.
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j > i and j < n:
                nxt = ITEM.match(lines[j])
                if nxt and len(nxt.group(1)) == base and nxt.group(2)[0].isdigit() == ordered:
                    i = j
                    items[-1].append("\0loose")
        loose = any("\0loose" in body for body in items)
        tag = "ol" if ordered else "ul"
        start = ""
        if ordered and first.group(2)[:-1] != "1":
            start = f' start="{int(first.group(2)[:-1])}"'
        rendered = []
        for body in items:
            body = [b for b in body if b != "\0loose"]
            tight = not loose and not any(not b.strip() for b in self.top_level_blanks(body))
            rendered.append("<li>" + self.blocks(body, tight=tight) + "</li>")
        out.append(f"<{tag}{start}>\n" + "\n".join(rendered) + f"\n</{tag}>")
        return i

    @staticmethod
    def top_level_blanks(body):
        """Blank lines outside fenced code and outside nested content."""
        fence = None
        found = []
        for line in body:
            m = FENCE.match(line)
            if fence is None and m:
                fence = m.group(1)
            elif fence is not None:
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                    fence = None
            elif not line.strip():
                found.append(line)
        # Only blank lines that separate top-level paragraphs make an item loose.
        return found if len(found) and sum(1 for line in body if line.strip() and not line.startswith(" ")) > 1 else []

    def render(self, page):
        self.page = page
        self.slugs = {}
        text = page.path.read_text(encoding="utf-8")
        page.body = self.blocks(text.splitlines())
        titles = [h for h in page.headings if h[0] == 1]
        page.title = titles[0][2] if titles else page.key
        first = re.search(r"<p>(.*?)</p>", page.body, re.S)
        if first:
            summary = html.unescape(re.sub(r"<[^>]+>", "", first.group(1)))
            summary = re.sub(r"\s+", " ", summary).strip()
            page.description = summary if len(summary) <= 200 else summary[:197].rsplit(" ", 1)[0] + "..."


# ---- page template -------------------------------------------------------------------

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta name="color-scheme" content="dark light">
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/site.css">
</head>
<body>
<header class="bar">
  <div class="bar-in">
    <a class="brand" href="/"><img src="/favicon.svg" alt="" width="24" height="24"><span>Tomacco</span></a>
    <nav class="dnav" aria-label="Site">
      <a href="/docs/" aria-current="page">Docs</a>
      <a href="/agents">Agents</a>
      <a href="/changelog">What's new</a>
      <a href="{repo}">GitHub</a>
    </nav>
  </div>
</header>
"""

FOOT = """<footer>
  <div class="foot">
    <p>Tomacco. Agent-first mobile development for Linux. &copy; 2026 Marsel Nenaj and contributors.</p>
    <p class="fl"><a href="/docs/">Docs</a> <a href="/changelog">What's new</a> <a href="/agents">Agents</a> <a href="/imprint">Imprint</a> <a href="/privacy">Privacy</a> <a href="/disclaimer">Disclaimer</a></p>
  </div>
</footer>
</body>
</html>
"""


def navigation(pages, current):
    seen = set()
    groups = []
    for title, keys in GROUPS:
        entries = [pages[k] for k in keys if k in pages]
        seen.update(k for k in keys if k in pages)
        groups.append((title, entries))
    rest = [p for k, p in sorted(pages.items()) if k not in seen and not k.startswith(("reference/", "virtual-ios/")) and k != "README"]
    if rest:
        groups.append(("More", rest))
    out = ['<nav class="docs-nav" aria-label="Documentation">',
           f'<a class="docs-home{" on" if current.key == "README" else ""}" href="{URL_BASE}/">All documentation</a>']
    for title, entries in groups:
        out.append(f"<h2>{html.escape(title)}</h2>\n<ul>")
        for p in entries:
            label = SHORT_TITLES.get(p.key, p.title)
            on = ' aria-current="page"' if p.key == current.key else ""
            out.append(f'<li><a href="{p.url}"{on}>{html.escape(label)}</a></li>')
            if p.key == "reference/README" and current.key.startswith("reference/"):
                subs = [q for k, q in sorted(pages.items()) if k.startswith("reference/") and k not in ("reference/README", "reference/errors")]
                out.append("<li><ul>" + "".join(
                    f'<li><a href="{q.url}"{" aria-current=page" if q.key == current.key else ""}>{html.escape(q.title)}</a></li>' for q in subs) + "</ul></li>")
        out.append("</ul>")
    out.append("</nav>")
    return "\n".join(out)


def contents(page):
    items = [(lvl, ident, text) for lvl, ident, text in page.headings if lvl in (2, 3)]
    if len(items) < 3:
        return ""
    rows = "\n".join(f'<li class="l{lvl}"><a href="#{ident}">{html.escape(text)}</a></li>' for lvl, ident, text in items)
    opened = " open" if len(items) <= 30 else ""
    return f'<details class="toc"{opened}>\n<summary>On this page ({len(items)})</summary>\n<ul>\n{rows}\n</ul>\n</details>'


def crumbs(page, pages):
    if page.key == "README":
        return ""
    parts = [f'<a href="{URL_BASE}/">Docs</a>']
    directory = posixpath.dirname(page.key)
    if directory and page.key != f"{directory}/README":
        index = pages.get(f"{directory}/README")
        if index:
            parts.append(f'<a href="{index.url}">{html.escape(index.title)}</a>')
    return '<p class="crumbs">' + " / ".join(parts) + "</p>"


def page_html(page, pages, ref):
    title = "Tomacco documentation" if page.key == "README" else f"{page.title}: Tomacco docs"
    head = HEAD.format(title=html.escape(title, quote=False), description=html.escape(page.description),
                       canonical=SITE + page.url, repo=REPO)
    edit = f"{REPO}/blob/{ref}/{page.source}"
    return (head + '<div class="docs">\n' + navigation(pages, page) + '\n<main class="docs-body" id="main">\n'
            + crumbs(page, pages) + "\n" + contents_after_title(page) + "\n"
            + f'<p class="source"><a href="{html.escape(edit)}">View this page as Markdown on GitHub</a></p>\n'
            + "</main>\n</div>\n" + FOOT)


def contents_after_title(page):
    """The body with the table of contents placed right after the first heading and its lead paragraph."""
    toc = contents(page)
    body = page.body
    if not toc:
        return body
    match = re.search(r"</h1>", body)
    if not match:
        return toc + "\n" + body
    end = match.end()
    para = re.match(r"\s*<p>.*?</p>", body[end:], re.S)
    if para:
        end += para.end()
    return body[:end] + "\n" + toc + body[end:]


def discover():
    pages = {}
    docs = ROOT / "docs"
    for path in sorted(docs.rglob("*.md")):
        key = path.relative_to(docs).with_suffix("").as_posix()
        pages[key] = Page(key, path)
    return pages


def build(out, ref="main", sitemap=None, lastmod=None):
    """Write the site docs into OUT. Returns the pages. Raises BuildError on any problem."""
    out = Path(out)
    pages = discover()
    images = set()
    renderer = Renderer(pages, ref, images)
    for page in pages.values():
        renderer.render(page)
    errors = list(renderer.errors)
    for page in pages.values():
        for key, fragment in page.links:
            if fragment and fragment not in pages[key].ids:
                errors.append(f"{page.source}: no anchor #{fragment} in {pages[key].source}")
    unplaced = [k for k in pages if k not in {x for _, ks in GROUPS for x in ks} and k != "README"
                and not k.startswith(("reference/", "virtual-ios/"))]
    if unplaced:
        errors.append("pages missing from GROUPS: " + ", ".join(unplaced))
    if errors:
        raise BuildError("\n".join(errors))
    if out.exists():
        for old in list(out.rglob("*.html")):
            old.unlink()
    for page in pages.values():
        target = out / page.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page_html(page, pages, ref), encoding="utf-8")
    for image in images:
        target = out / image.relative_to(ROOT / "docs")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, target)
    if sitemap:
        update_sitemap(Path(sitemap), pages, lastmod or datetime.date.today().isoformat())
    return pages


def update_sitemap(path, pages, lastmod):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"[ \t]*<url><loc>" + re.escape(SITE + URL_BASE) + r"[^<]*</loc>[^\n]*\n", "", text)
    urls = "".join(f"  <url><loc>{SITE}{p.url}</loc><lastmod>{lastmod}</lastmod></url>\n"
                   for p in sorted(pages.values(), key=lambda p: p.url))
    path.write_text(text.replace("</urlset>", urls + "</urlset>"), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("output", help="the site's docs/ directory")
    parser.add_argument("--ref", default="main", help="Git ref used for links to files that are not pages (default main)")
    parser.add_argument("--sitemap", help="a sitemap.xml to add the pages to")
    parser.add_argument("--lastmod", help="lastmod date for the sitemap (default today)")
    args = parser.parse_args(argv)
    try:
        pages = build(args.output, args.ref, args.sitemap, args.lastmod)
    except BuildError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"wrote {len(pages)} pages to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
