/* 公式（KaTeX）与 mermaid 图的页面端渲染。
 * 模板只在正文真的包含 $$…$$ / \[…\] / \(…\) 或 ```mermaid 块时才加载对应库，
 * 这个文件总是最后 defer 加载。
 *
 * 两者的加载策略不同：
 * - KaTeX（200 多 KB）照旧 eager：公式就是正文文字，晚渲染会让读者先看到
 *   $$x^2$$ 这样的源码；
 * - mermaid 是 2.7MB 的大件，由这里在**首屏画完之后**再按需拉取：它的下载
 *   不再和关键资源抢带宽，也不再把 load 事件拖到它下完为止。图区在渲染前
 *   本来就靠 .mermaid{line-height:0} 留白，观感与之前一致。
 *
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

  // 按需加载 mermaid（同一页面只拉一次，失败也记住，避免反复重试）
  var mermaidPromise = null;

  function loadMermaid() {
    if (window.mermaid) { return Promise.resolve(true); }
    if (mermaidPromise) { return mermaidPromise; }
    var src = window.__inknoteMermaidSrc;
    if (!src) { mermaidPromise = Promise.resolve(false); return mermaidPromise; }
    mermaidPromise = new Promise(function (resolve) {
      var tag = document.createElement('script');
      tag.src = src;
      tag.async = true;
      tag.onload = function () { resolve(!!window.mermaid); };
      tag.onerror = function () { resolve(false); };
      document.head.appendChild(tag);
    });
    return mermaidPromise;
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

  // 首屏画完（load 事件）再拉 mermaid；用 rIC 让它在空闲时段开始，最多等 1.5s
  function scheduleMermaid() {
    if (!document.querySelector('.mermaid')) { return; }
    var go = function () {
      loadMermaid().then(function (ok) {
        if (!ok) { return; }   // 拉不到就保持留白，不抛错
        try { renderMermaid(); } catch (err) { /* 图失败不影响公式 */ }
      });
    };
    var afterLoad = function () {
      if (window.requestIdleCallback) { window.requestIdleCallback(go, { timeout: 1500 }); }
      else { window.setTimeout(go, 250); }
    };
    if (document.readyState === 'complete') { afterLoad(); }
    else { window.addEventListener('load', afterLoad, { once: true }); }
  }

  function run() {
    try { renderMath(); } catch (err) { /* 公式失败不影响图 */ }
    try { scheduleMermaid(); } catch (err) { /* 图失败不影响公式 */ }
  }

  if (document.readyState !== 'loading') {
    run();
  } else {
    document.addEventListener('DOMContentLoaded', run);
  }
})();
