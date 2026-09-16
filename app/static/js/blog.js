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

  /* ============ 3) TOC 滚动高亮（scroll-spy） ============ */
  function setupTocSpy() {
    var toc = document.querySelector('.toc[data-toc]');
    var body = document.querySelector('.post-body');
    if (!toc || !body) { return; }
    var links = toc.querySelectorAll('.toc__link');
    if (!links.length) { return; }

    var map = {};
    Array.prototype.forEach.call(links, function (link) {
      var href = link.getAttribute('href') || '';
      var id = href.charAt(0) === '#' ? href.slice(1) : '';
      if (id) { map[id] = link; }
    });

    var current = null;
    function setActive(id) {
      var link = map[id];
      if (!link || link === current) { return; }
      if (current) { current.classList.remove('is-active'); }
      link.classList.add('is-active');
      current = link;
    }

    var headings = body.querySelectorAll('h1[id], h2[id], h3[id], h4[id]');
    if (!headings.length) { return; }

    if (typeof window.IntersectionObserver !== 'function') {
      // 老浏览器退化：哪个小节的链接被点击就亮哪个
      Array.prototype.forEach.call(links, function (link) {
        link.addEventListener('click', function () {
          setActive(decodeURIComponent((link.getAttribute('href') || '').slice(1)));
        });
      });
      return;
    }

    // 视口上 1/5 到下 1/3 之间的小节视为「正在读」
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { setActive(entry.target.id); }
      });
    }, { rootMargin: '-15% 0px -65% 0px' });
    Array.prototype.forEach.call(headings, function (h) { observer.observe(h); });
  }

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

  ready(function () {
    try { setupLike(); } catch (error) { /* 单功能失败不拖垮其它 */ }
    try { setupLightbox(); } catch (error) { /* 同上 */ }
    try { setupTocSpy(); } catch (error) { /* 同上 */ }
    try { setupReveal(); } catch (error) { /* 同上 */ }
  });
})();
