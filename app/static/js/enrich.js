/* 公式（KaTeX）与 mermaid 图的页面端渲染。
 * 模板只在正文真的包含 $$…$$ / \[…\] / \(…\) 或 ```mermaid 块时才加载对应库，
 * 这个文件总是最后 defer 加载，所以执行时 window.katex / window.mermaid 一定就绪。
 * 正文 HTML 由服务端转义过，这里只做增强，不做任何注入。 */
(function () {
  'use strict';

  function renderMath() {
    if (typeof window.renderMathInElement !== 'function') { return; }
    var options = {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false }
      ],
      // 代码块和 mermaid 源码里的美元符号 / 反斜杠不参与公式
      ignoredClasses: ['codehilite', 'mermaid'],
      throwOnError: false
    };
    var bodies = document.querySelectorAll('.prose');
    Array.prototype.forEach.call(bodies, function (el) {
      window.renderMathInElement(el, options);
    });
  }

  function renderMermaid() {
    var blocks = document.querySelectorAll('.mermaid');
    if (!blocks.length || typeof window.mermaid === 'undefined') { return; }
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: dark ? 'dark' : 'default'
    });
    // 单块逐个渲染：一张图画不出来不能拖垮其它图。
    // 注意 v9 的 init 只认单个元素或 NodeList，传 [el] 数组会静默不渲染。
    Array.prototype.forEach.call(blocks, function (el) {
      if (el.getAttribute('data-mermaid-done')) { return; }
      el.setAttribute('data-mermaid-done', '1');
      try {
        window.mermaid.init(undefined, el);
      } catch (err) {
        el.classList.add('mermaid-error');
        el.removeAttribute('data-processed');
      }
    });
  }

  function run() {
    try { renderMath(); } catch (err) { /* 公式失败不影响图 */ }
    try { renderMermaid(); } catch (err) { /* 图失败不影响公式 */ }
  }

  if (document.readyState !== 'loading') {
    run();
  } else {
    document.addEventListener('DOMContentLoaded', run);
  }
})();
