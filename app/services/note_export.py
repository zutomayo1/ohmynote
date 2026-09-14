"""单文件精美导出：把一篇笔记渲染成自包含的 HTML（内嵌样式，可直接分发）。

导出文件不依赖站点资源：配色内联、亮暗跟随系统、打印友好。
"""

import datetime as _dt
import html as _html

_EXPORT_CSS = """
:root { --bg:#FAF6EF; --surface:#FFFDF8; --ink:#2E2A26; --ink-2:#5C5348; --ink-3:#8B8073;
        --line:#E6DCCB; --brand:#B5533C; --surface-2:#F7F1E7; --code-bg:#322C26; --code-ink:#E8DFD2; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1F1B17; --surface:#2A241E; --ink:#EDE4D6; --ink-2:#C6B9A6; --ink-3:#9A8D7C;
          --line:#3D342C; --brand:#D98872; --surface-2:#332B24; --code-bg:#161210; --code-ink:#D9CFBE; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); line-height:1.8;
       font:17px/1.8 Georgia,"Source Serif Pro","Noto Serif SC","Songti SC",serif;
       -webkit-font-smoothing:antialiased; }
main { max-width: 46rem; margin: 0 auto; padding: 56px 22px 72px; }
h1 { font-size: 30px; line-height: 1.3; margin: 0 0 6px; }
.meta { color: var(--ink-3); font-size: 14px; margin-bottom: 30px; }
.meta .brand { color: var(--brand); }
hr.thick { border: none; height: 3px; width: 44px; border-radius: 99px;
           background: var(--brand); margin: 14px 0 34px; }
.prose h1,.prose h2,.prose h3 { line-height: 1.4; margin: 1.6em 0 .6em; }
.prose h2 { font-size: 24px; } .prose h3 { font-size: 20px; }
.prose p { margin: 0 0 1.1em; }
.prose a { color: var(--brand); }
.prose ul,.prose ol { padding-left: 1.4em; margin: 0 0 1.1em; }
.prose blockquote { margin: 1.2em 0; padding: .2em 0 .2em 1em;
  border-left: 3px solid var(--brand); color: var(--ink-2); }
.prose code { font-family: ui-monospace,Consolas,monospace; font-size: .88em;
  background: var(--surface-2); padding: .1em .35em; border-radius: 5px; }
.prose pre { background: var(--code-bg); color: var(--code-ink); padding: 14px 16px;
  border-radius: 10px; overflow-x: auto; font-size: 14px; }
.prose pre code { background: transparent; color: inherit; padding: 0; }
.prose img { max-width: 100%; border-radius: 10px; }
.prose table { border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: 15px; }
.prose th,.prose td { border: 1px solid var(--line); padding: 7px 11px; text-align: left; }
.prose th { background: var(--surface-2); }
.prose tr:nth-child(even) td { background: color-mix(in srgb, var(--surface-2) 55%, transparent); }
footer { margin-top: 60px; padding-top: 16px; border-top: 1px solid var(--line);
  color: var(--ink-3); font-size: 13px; }
@media print { body { background: #fff; } main { padding: 0; } footer { display: none; } }
"""


def build_export_html(note: dict, rendered_html: str) -> str:
    """把渲染后的正文装进自包含 HTML。note/正文内容均已由调用方保证安全。"""
    title = _html.escape(str(note.get("title") or "无标题笔记"))
    today = _dt.date.today().isoformat()
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{_EXPORT_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{title}</h1>\n"
        f'<hr class="thick">\n'
        f'<div class="prose">\n{rendered_html}\n</div>\n'
        f'<footer>导出自 <span class="brand">墨痕 InkNote</span> · {today}</footer>\n'
        "</main>\n</body>\n</html>\n"
    )
