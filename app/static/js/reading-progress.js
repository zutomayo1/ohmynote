/* 博客阅读进度条：仅在「文章页」生效（存在 .post-body 或 article.post）。
 * 自己创建 DOM（不依赖模板改动），贴在吸顶站头 (.site-header) 的下沿作为 2px 细线。
 * 进度 = 正文已在视口内滚过的比例（相对文章自身高度，非整页），随滚动增长，读完到底淡出。
 * 零依赖、IIFE、'use strict'；用 requestAnimationFrame 合并 scroll + passive 监听，
 * 绝不在每帧读写布局（只在事件里算一次），用 transform: scaleX() 驱动（不做 width 动画）。
 */
(function () {
  'use strict';

  // 幂等守卫：这个文件可能被加载两次（页面 defer 引用 + 动态注入），
  // 第二次执行必须直接退出，否则会建出两条进度条、监听器也翻倍。
  if (window.__inknoteReadingProgress) { return; }
  window.__inknoteReadingProgress = true;

  var BAR_ID = 'reading-progress';
  var VISIBLE_CLASS = 'reading-progress--visible';
  var MIN_ARTICLE_RATIO = 0.9; // 正文不足一屏的 90% 时视为「太短」，不显示
  var FADE_THRESHOLD = 0.99;   // 读到 99% 以上视为到底，淡出

  function ready(run) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', run, { once: true });
    } else {
      run();
    }
  }

  function init() {
    // 1) 只在文章页初始化：先找正文容器 .post-body，退而求其次 article.post
    var article = document.querySelector('.post-body') ||
      document.querySelector('article.post');
    if (!article) return;

    var header = document.querySelector('.site-header');
    var headerH = header ? header.offsetHeight : 0;

    // 文章相对进度：正文顶部滚到视口顶部（避开吸顶站头）时为 0，
    // 正文底部滚到视口底部时为 1。不足一屏返回 -1（不显示）。
    function compute() {
      var vh = window.innerHeight || document.documentElement.clientHeight;
      var articleH = article.offsetHeight;
      if (articleH <= vh * MIN_ARTICLE_RATIO) return -1;

      var rect = article.getBoundingClientRect();
      var articleTop = rect.top + (window.pageYOffset || document.documentElement.scrollTop || 0);
      var start = articleTop - headerH;          // 正文顶刚到站的下方
      var denom = articleH - vh;                 // 需要滚动的距离
      if (denom <= 0) return -1;

      var p = ((window.pageYOffset || document.documentElement.scrollTop || 0) - start) / denom;
      if (p < 0) return 0;
      if (p > 1) return 1;
      return p;
    }

    // 2) 先量一次，太短就直接不建 DOM（满足「短文不显示」）
    if (compute() < 0) return;

    // 3) 自建 DOM：容器贴在站头下沿，填充条用 scaleX 伸缩
    var container = document.createElement('div');
    container.id = BAR_ID;
    container.className = 'reading-progress';
    container.setAttribute('aria-hidden', 'true');

    var bar = document.createElement('div');
    bar.className = 'reading-progress__bar';
    container.appendChild(bar);

    if (header) {
      header.appendChild(container);   // 跟随站头，零抖动；pointer-events:none 不挡点击
    } else {
      document.body.appendChild(container);
      container.style.position = 'fixed';
      container.style.top = 'var(--header-h)';
    }

    var ticking = false;
    function schedule() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        ticking = false;
        update();
      });
    }

    function update() {
      var p = compute();
      if (p < 0) {
        container.classList.remove(VISIBLE_CLASS);
        return;
      }
      // 只在合成帧里写一次 transform，不做每帧布局读写
      bar.style.transform = 'scaleX(' + p + ')';
      if (p >= FADE_THRESHOLD) {
        container.classList.remove(VISIBLE_CLASS); // 到底淡出
      } else {
        container.classList.add(VISIBLE_CLASS);    // 滚回顶部会重新出现
      }
    }

    // 4) 被动监听 + rAF 合并，避免每个滚动事件都触发布局
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule, { passive: true });
    update();
  }

  ready(init);
})();
