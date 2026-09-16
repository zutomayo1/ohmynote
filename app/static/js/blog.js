/* 博客阅读增强：点赞、图片灯箱、TOC 滚动高亮、文章卡入场动效。
 *
 * 自守卫：哪个功能的前置元素不存在就跳过，内页/其它站点页面零影响。
 * 语言标签由服务端（markdown_render._fence_format）渲染，这里不管代码块。
 * 全部渐进增强：无 JS 时点赞走普通表单提交，图片/目录照常可用。
 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
      fn();
    }
  }

  var reduceMotion = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* ============ 1) 点赞：乐观更新 + localStorage 防重 ============ */
  function setupLike() {
    var box = document.querySelector('.post-like');
    if (!box) { return; }
    var btn = box.querySelector('.post-like__btn');
    var count = box.querySelector('.post-like__count');
    var slug = box.getAttribute('data-slug');
    if (!btn || !slug) { return; }

    var KEY = 'inknote-liked:' + slug;
    var liked = false;
    try { liked = localStorage.getItem(KEY) === '1'; } catch (error) { /* 隐私模式 */ }
    if (liked) {
      btn.classList.add('is-liked');
      btn.disabled = true;
    }

    btn.addEventListener('click', function (event) {
      if (liked) { return; }
      event.preventDefault();
      fetch('/blog/' + encodeURIComponent(slug) + '/like', { method: 'POST' })
        .then(function (resp) {
          if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
          return resp.json();
        })
        .then(function (data) {
          if (!data || !data.ok) { throw new Error('bad payload'); }
          liked = true;
          try { localStorage.setItem(KEY, '1'); } catch (error) { /* 忽略 */ }
          btn.classList.add('is-liked');
          btn.disabled = true;
          if (count) { count.textContent = String(data.likes); }
        })
        .catch(function () {
          // fetch 走不通（如旧浏览器/代理拦截）：退回传统表单提交，页面刷新拿计数
          if (btn.form) { btn.form.submit(); }
        });
    });
  }

  /* ============ 2) 图片灯箱 ============ */
  var lightbox = null;
  var lightboxImg = null;
  var lightboxCap = null;

  function buildLightbox() {
    lightbox = document.createElement('div');
    lightbox.className = 'lightbox';
    lightbox.setAttribute('role', 'dialog');
    lightbox.setAttribute('aria-modal', 'true');
    lightbox.setAttribute('aria-label', '图片预览');

    var figure = document.createElement('figure');
    lightboxImg = document.createElement('img');
    lightboxImg.className = 'lightbox__img';
    lightboxImg.alt = '';
    var caption = document.createElement('figcaption');
    caption.className = 'lightbox__cap';
    lightboxCap = caption;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'lightbox__close';
    close.textContent = '×';
    close.setAttribute('aria-label', '关闭预览');

    figure.appendChild(lightboxImg);
    figure.appendChild(caption);
    lightbox.appendChild(figure);
    lightbox.appendChild(close);
    document.body.appendChild(lightbox);

    lightbox.addEventListener('click', function (event) {
      // 点空白 / 点关闭都退出；点图片本身不退出（方便看局部）
      if (event.target === lightboxImg) { return; }
      closeLightbox();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && lightbox.classList.contains('is-open')) {
        closeLightbox();
      }
    });
  }

  function openLightbox(img) {
    if (!lightbox) { buildLightbox(); }
    lightboxImg.src = img.currentSrc || img.src;
    lightboxImg.alt = img.alt || '';
    var text = (img.alt || '').trim();
    lightboxCap.textContent = text;
    lightboxCap.style.display = text ? '' : 'none';
    lightbox.classList.add('is-open');
  }

  function closeLightbox() {
    if (!lightbox) { return; }
    lightbox.classList.remove('is-open');
    // 清 src，避免关掉后大图还占着解码缓存
    window.setTimeout(function () {
      if (!lightbox.classList.contains('is-open')) { lightboxImg.removeAttribute('src'); }
    }, 200);
  }

  function setupLightbox() {
    var body = document.querySelector('.post-body');
    if (!body) { return; }
    body.addEventListener('click', function (event) {
      var img = event.target && event.target.closest ? event.target.closest('img') : null;
      if (!img || !body.contains(img)) { return; }
      if (img.closest('a')) { return; }   // 已包链接的图：让链接行为优先
      openLightbox(img);
    });
  }

  /* ============ 3) TOC 滚动高亮：app.js 的 initToc 已实现（阈值上方最靠下），
        这里不重复做——两个 spy 会互相覆盖 is-active。 ============ */

  /* ============ 4) 文章卡入场动效（featured 渐显更慢更深） ============ */
  function setupReveal() {
    var list = document.querySelector('.blog-list');
    if (!list || reduceMotion) { return; }
    var cards = list.querySelectorAll('.post-card');
    if (!cards.length) { return; }

    if (typeof window.IntersectionObserver !== 'function') {
      Array.prototype.forEach.call(cards, function (card) { card.classList.add('is-inview'); });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        entry.target.classList.add('is-inview');
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

    Array.prototype.forEach.call(cards, function (card, index) {
      // 首屏卡片错峰入场，封顶 360ms 避免后面的卡等太久
      card.style.transitionDelay = Math.min(index * 60, 360) + 'ms';
      observer.observe(card);
    });
  }

  /* ============ 5) 归档时间轴条目入场 ============ */
  function setupTimelineReveal() {
    var timeline = document.querySelector('.timeline');
    if (!timeline || reduceMotion) { return; }
    var items = timeline.querySelectorAll('.timeline__item');
    if (!items.length) { return; }

    if (typeof window.IntersectionObserver !== 'function') {
      Array.prototype.forEach.call(items, function (item) { item.classList.add('is-inview'); });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        entry.target.classList.add('is-inview');
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.1 });

    Array.prototype.forEach.call(items, function (item, index) {
      // 首屏条目按序错峰淡入
      item.style.transitionDelay = Math.min(index * 40, 280) + 'ms';
      observer.observe(item);
    });
  }


  /* ============ 6) 专注阅读模式（仅博客文章页）：隐藏两侧与附属区块 ============ */
  function setupFocusMode() {
    if (document.body.dataset.page !== 'post') { return; }

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'focus-toggle';
    btn.setAttribute('aria-pressed', 'false');
    btn.title = '专注阅读（Esc 退出）';
    // 自绘「取景框」符号：四角括号，表示进入沉浸画面
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M8.5 3.5H5A1.5 1.5 0 0 0 3.5 5v3.5"/>' +
      '<path d="M15.5 3.5H19A1.5 1.5 0 0 1 20.5 5v3.5"/>' +
      '<path d="M8.5 20.5H5A1.5 1.5 0 0 1 3.5 19V15.5"/>' +
      '<path d="M15.5 20.5H19a1.5 1.5 0 0 0 1.5-1.5V15.5"/>' +
      '</svg><span class="focus-toggle__text">专注</span>';
    var btnText = btn.querySelector('.focus-toggle__text');

    function paint() {
      var on = document.body.classList.contains('focus-reading');
      if (btnText) { btnText.textContent = on ? '退出专注' : '专注'; }
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }

    // 丝滑切换：开启 = 先让页头/侧栏淡出滑出，再 display 收起；
    // 关闭 = 先移除 display，下一帧起播淡入。
    var LEAVE_MS = 240;
    var leaving = false;
    function setFocus(on) {
      if (leaving) { return; }
      if (on) {
        leaving = true;
        document.body.classList.add('focus-leaving');
        window.setTimeout(function () {
          document.body.classList.remove('focus-leaving');
          document.body.classList.add('focus-reading');
          leaving = false;
          paint();
        }, LEAVE_MS);
      } else {
        document.body.classList.add('focus-entering');
        document.body.classList.remove('focus-reading');
        paint();
        window.requestAnimationFrame(function () {
          window.requestAnimationFrame(function () {
            document.body.classList.remove('focus-entering');
          });
        });
      }
    }

    btn.addEventListener('click', function () {
      setFocus(!document.body.classList.contains('focus-reading'));
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && document.body.classList.contains('focus-reading')) {
        setFocus(false);
      }
    });

    document.body.appendChild(btn);
    paint();
  }

  /* ============ 7) 博客左栏分类树：记住展开/折叠状态 ============ */
  function setupSideTree() {
    var cats = document.querySelectorAll('.post-side__cat[data-cat]');
    if (!cats.length) { return; }
    var KEY = 'inknote-side-tree';
    var saved = null;
    try { saved = JSON.parse(window.localStorage.getItem(KEY) || 'null'); } catch (error) { saved = null; }
    if (saved && typeof saved === 'object') {
      Array.prototype.forEach.call(cats, function (d) {
        var name = d.getAttribute('data-cat');
        if (name in saved) { d.open = !!saved[name]; }
      });
    }
    Array.prototype.forEach.call(cats, function (d) {
      d.addEventListener('toggle', function () {
        var map = {};
        Array.prototype.forEach.call(cats, function (x) {
          map[x.getAttribute('data-cat')] = x.open;
        });
        try { window.localStorage.setItem(KEY, JSON.stringify(map)); } catch (error) { /* 忽略 */ }
      });
    });
  }

  ready(function () {
    try { setupLike(); } catch (error) { /* 单功能失败不拖垮其它 */ }
    try { setupLightbox(); } catch (error) { /* 同上 */ }
    try { setupReveal(); } catch (error) { /* 同上 */ }
    try { setupTimelineReveal(); } catch (error) { /* 同上 */ }
    try { setupFocusMode(); } catch (error) { /* 同上 */ }
    try { setupSideTree(); } catch (error) { /* 同上 */ }
  });
})();
