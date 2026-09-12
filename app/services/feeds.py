"""RSS / Atom 订阅源与 sitemap、robots.txt。

全部用标准库 xml.etree 生成，转义交给 ElementTree，避免手拼 XML 出错。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Iterable

from ..config import settings
from ..utils import now, parse_dt

ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


def rfc3339(value: str | None = None) -> str:
    """转成 RFC3339；传入 None 表示「现在」。"""
    dt = parse_dt(value) if value else None
    if dt is None:
        dt = now()
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 视作本机时区，补上偏移量
    return dt.replace(microsecond=0).isoformat()


def absolute(path: str) -> str:
    if not path:
        return settings.base_url
    if path.startswith(("http://", "https://")):
        return path
    return f"{settings.base_url}{path if path.startswith('/') else '/' + path}"


def _xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def atom_feed(entries: Iterable[dict[str, Any]]) -> bytes:
    """Atom 1.0。entries: [{note, html}]"""
    feed = ET.Element("feed", {"xmlns": ATOM_NS})
    ET.SubElement(feed, "title").text = settings.site_title
    ET.SubElement(feed, "subtitle").text = settings.site_subtitle or settings.site_description
    ET.SubElement(feed, "id").text = absolute("/blog")
    ET.SubElement(feed, "updated").text = rfc3339(None)
    ET.SubElement(feed, "link", {"rel": "self", "type": "application/atom+xml", "href": absolute("/feed.xml")})
    ET.SubElement(feed, "link", {"rel": "alternate", "type": "text/html", "href": absolute("/blog")})
    feed.append(_author_name())

    for item in entries:
        note = item["note"]
        url = absolute(note.get("blog_url") or f"/blog/{note.get('slug')}")
        entry = ET.SubElement(feed, "entry")
        ET.SubElement(entry, "title").text = note.get("title") or "无标题"
        ET.SubElement(entry, "id").text = url
        ET.SubElement(entry, "link", {"rel": "alternate", "type": "text/html", "href": url})
        ET.SubElement(entry, "published").text = rfc3339(note.get("published_at") or note.get("created_at"))
        ET.SubElement(entry, "updated").text = rfc3339(note.get("updated_at"))
        summary = ET.SubElement(entry, "summary")
        summary.text = note.get("summary") or ""
        content = ET.SubElement(entry, "content", {"type": "html"})
        content.text = item.get("html") or ""
        for tag in note.get("tags") or []:
            ET.SubElement(entry, "category", {"term": tag})
        entry.append(_author_name())

    return _xml(feed)


def _author_name() -> ET.Element:
    author = ET.Element("author")
    ET.SubElement(author, "name").text = settings.author or settings.site_title
    return author


def rss_feed(entries: Iterable[dict[str, Any]]) -> bytes:
    """RSS 2.0（正文放在 content:encoded，摘要放 description）。"""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = settings.site_title
    ET.SubElement(channel, "link").text = absolute("/blog")
    ET.SubElement(channel, "description").text = settings.site_subtitle or settings.site_description
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = rfc3339(None)
    ET.SubElement(channel, "generator").text = "InkNote"

    for item in entries:
        note = item["note"]
        url = absolute(note.get("blog_url") or f"/blog/{note.get('slug')}")
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = note.get("title") or "无标题"
        ET.SubElement(node, "link").text = url
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = url
        ET.SubElement(node, "pubDate").text = rfc3339(note.get("published_at") or note.get("created_at"))
        ET.SubElement(node, "description").text = note.get("summary") or ""
        # 用 QName 写法，配合 register_namespace 序列化成 <content:encoded>
        ET.SubElement(node, f"{{{CONTENT_NS}}}encoded").text = item.get("html") or ""
        for tag in note.get("tags") or []:
            ET.SubElement(node, "category").text = tag

    return _xml(rss)


def sitemap(entries: Iterable[dict[str, Any]], extra_paths: Iterable[str] = ()) -> bytes:
    urlset = ET.Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    paths = list(extra_paths)
    for path in paths:
        node = ET.SubElement(urlset, "url")
        ET.SubElement(node, "loc").text = absolute(path)
    for item in entries:
        note = item["note"]
        node = ET.SubElement(urlset, "url")
        ET.SubElement(node, "loc").text = absolute(note.get("blog_url") or f"/blog/{note.get('slug')}")
        ET.SubElement(node, "lastmod").text = (note.get("updated_at") or "").replace(" ", "T")[:10]
        ET.SubElement(node, "changefreq").text = "weekly"
        ET.SubElement(node, "priority").text = "0.8"
    return _xml(urlset)


def robots_txt() -> str:
    return "\n".join(
        [
            "User-agent: *",
            "Allow: /blog",
            "Allow: /media/",
            "Allow: /feed.xml",
            "Allow: /rss.xml",
            "Disallow: /notes",
            "Disallow: /trash",
            "Disallow: /templates",
            "Disallow: /search",
            "Disallow: /ask",
            "Disallow: /api/",
            "Disallow: /export/",
            "Disallow: /login",
            "",
            f"Sitemap: {settings.base_url}/sitemap.xml",
            "",
        ]
    )
