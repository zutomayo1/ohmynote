/* 关系图谱：零依赖自绘 SVG 力导向图（一个可复用引擎，全图页与笔记页局部图共用）。
 *
 * 借鉴（只借算法与交互设计，不引任何库）：
 * - d3-force：节点碰撞（collide）、标签避让（密集时只给大节点/悬停节点显示标题）、
 *   力的分层（斥力 / 链接 / 居中）与 alpha 衰减收敛；
 * - graphology-layout-forceatlas2：参数化布局（斥力强度 ≈ scalingRatio、
 *   阻尼 ≈ slowDown、居中 ≈ gravity），并给出 Barnes–Hut 降复杂度的做法；
 * - force-graph / sigma.js：按度数缩放节点、滚轮缩放与拖拽平移的手感、
 *   双向边画成两条分离的弧线；
 * - Obsidian / Quartz：局部图（只看一篇笔记的邻居）这一形态。
 *
 * 自研要点：
 * - 斥力：节点数 < BH_THRESHOLD 时精确两两计算；超过则用四叉树做 Barnes–Hut 近似
 *   （θ=0.5，O(n log n)），两套路径的力的量纲一致，所以布局参数通用；
 * - 位置记忆：把每个节点的坐标存进 localStorage（按 note id），刷新后从原位
 *   短程收敛（而不是重新乱排），因此布局基本稳定；
 * - 着色：按 Louvain 社区（后端算）或按第一个标签的色组（与标签药丸同一色板）。
 *
 * 类名一律用 setAttribute('class', …) 设置（SVG 标准做法；HTML 图例元素除外，
 * 它们的类名在 style.css 里有样式，供 test_js_class_guard 校验）。
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var PALETTE = 8;              // 色板档数（与标签色组一致）
  var R_MIN = 4.5;              // 最小节点半径
  var R_MAX = 15;               // 最大节点半径（连接数最多的那个）
  var BH_THRESHOLD = 200;       // 超过这个节点数改用 Barnes–Hut
  var THETA = 0.5;              // Barnes–Hut 精度

  // 物理参数（对标 ForceAtlas2 的参数划分）
  var REPULSION = 4200;         // 斥力强度
  var SPRING_LEN = 96;          // 边的理想长度
  var SPRING_K = 0.035;         // 弹簧刚度
  var CENTER_K = 0.012;         // 向中心的引力
  var DAMPING = 0.86;           // 阻尼
  var MAX_V = 18;               // 单步最大位移
  var COLLIDE_PAD = 6;          // 碰撞间隙
  var HOME = 2000;              // 坐标软边界（防止失控飞出宇宙）
  var STOP_MS = 2500;           // 首次布局的时长上限
  var RESUME_MS = 900;          // 从记忆位置恢复时的时长（只做局部微调）
  var POS_KEY = "inknote.graph.pos.v1";
  var PARAM_KEY = "inknote.graph.params.v1";   // 布局参数（用户在面板上调的松紧）
  var PARAM_DEFAULTS = { repulsion: 100, spring: 100, center: 100 };   // 百分比
  var PARAM_LIMITS = { repulsion: [40, 240], spring: [50, 220], center: [20, 300] };
  var PARAM_KEYS = ["repulsion", "spring", "center"];

  /* ------------------------------------------------------------------ 工具 */
  function setClass(el, cls) { el.setAttribute("class", cls); }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function rand(a, b) { return a + Math.random() * (b - a); }

  function readPayload(el) {
    if (!el) { return null; }
    var raw = el.textContent || el.innerText || "";
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  /* 位置记忆（一个全局表：note id → [x, y]），节点增删后仍能复用 */
  function loadPositions() {
    try {
      var raw = window.localStorage.getItem(POS_KEY);
      var data = raw ? JSON.parse(raw) : null;
      return data && typeof data === "object" ? data : {};
    } catch (e) { return {}; }
  }

  /* 布局参数：斥力 / 连线长度 / 向心力，都是相对基准值的百分比。
   * 调完存 localStorage，下次进来还是你习惯的松紧。 */
  function loadParams() {
    var out = {};
    PARAM_KEYS.forEach(function (k) { out[k] = PARAM_DEFAULTS[k]; });
    try {
      var raw = window.localStorage.getItem(PARAM_KEY);
      if (!raw) { return out; }
      var saved = JSON.parse(raw);
      PARAM_KEYS.forEach(function (k) {
        var v = Number(saved && saved[k]);
        if (isFinite(v)) {
          out[k] = clamp(Math.round(v), PARAM_LIMITS[k][0], PARAM_LIMITS[k][1]);
        }
      });
    } catch (e) { /* 隐私模式 / 坏数据：用默认值 */ }
    return out;
  }

  var persistTimer = null;
  function saveParams(params) {
    try { window.localStorage.setItem(PARAM_KEY, JSON.stringify(params)); } catch (e) { /* 忽略 */ }
  }
  // 拖动滑杆会连续触发，写 localStorage 做个防抖
  function schedulePersist(params) {
    if (persistTimer) { clearTimeout(persistTimer); }
    persistTimer = setTimeout(function () { saveParams(params); }, 400);
  }

  function savePositions(sim) {
    try {
      var out = {};
      var count = 0;
      for (var id in sim) {
        if (!Object.prototype.hasOwnProperty.call(sim, id)) { continue; }
        var p = sim[id];
        out[id] = [Math.round(p.x), Math.round(p.y)];
        count += 1;
        if (count > 3000) { break; }   // 上限，别把 localStorage 撑爆
      }
      window.localStorage.setItem(POS_KEY, JSON.stringify(out));
    } catch (e) { /* 隐私模式 / 配额满：静默降级 */ }
  }

  /* ------------------------------------------------- Barnes–Hut 四叉树 */
  function makeQuad(cx, cy, half) {
    return { cx: cx, cy: cy, half: half, mass: 0, mx: 0, my: 0, kids: null, body: null };
  }

  function quadIndex(node, body) {
    return (body.x >= node.cx ? 1 : 0) + (body.y >= node.cy ? 2 : 0);
  }

  function splitQuad(node) {
    var h = node.half / 2;
    return [
      makeQuad(node.cx - h, node.cy - h, h),
      makeQuad(node.cx + h, node.cy - h, h),
      makeQuad(node.cx - h, node.cy + h, h),
      makeQuad(node.cx + h, node.cy + h, h)
    ];
  }

  function quadInsert(node, body) {
    var m0 = node.mass;
    node.mass = m0 + body.mass;
    if (node.mass > 0) {
      node.mx = (node.mx * m0 + body.x * body.mass) / node.mass;
      node.my = (node.my * m0 + body.y * body.mass) / node.mass;
    }
    if (!node.kids && !node.body) { node.body = body; return; }
    if (!node.kids) {
      var old = node.body;
      node.body = null;
      node.kids = splitQuad(node);
      if (old) { quadDescend(node, old); }
    }
    quadDescend(node, body);
  }

  function quadDescend(node, body) {
    var kid = node.kids[quadIndex(node, body)];
    if (kid.half < 0.5) { return; }   // 深度上限（重合点）：质心已在祖先层计过
    quadInsert(kid, body);
  }

  function quadForce(node, body, strength, force) {
    if (node.mass === 0) { return; }
    if (!node.kids && node.body === body) { return; }   // 自己
    var dx = body.x - node.mx;
    var dy = body.y - node.my;
    var d2 = dx * dx + dy * dy;
    var size = node.half * 2;
    if (!node.kids || size * size <= THETA * THETA * d2) {
      if (d2 < 1e-6) { dx = rand(-1, 1); dy = rand(-1, 1); d2 = 1; }
      var d = Math.sqrt(d2);
      var f = strength * node.mass / d2;
      force.x += (dx / d) * f;
      force.y += (dy / d) * f;
      return;
    }
    for (var i = 0; i < 4; i++) { quadForce(node.kids[i], body, strength, force); }
  }

  /* ============================================================ 引擎 */
  function create(stage, payload, options) {
    if (!stage || !payload) { return null; }
    var opts = options || {};
    var nodes = payload.nodes || [];
    var edges = payload.edges || [];
    var allTags = [];
    (function () {
      var seen = {};
      nodes.forEach(function (n) {
        (n.tags || []).forEach(function (t) {
          if (!seen[t]) { seen[t] = 1; allTags.push(t); }
        });
      });
      allTags.sort();
    })();

    var svg = stage.querySelector("svg");
    if (!svg) {
      svg = document.createElementNS(SVGNS, "svg");
      svg.setAttribute("id", stage.id ? stage.id + "-svg" : "graph-svg");
      setClass(svg, "graph-svg");
      stage.appendChild(svg);
    }

    var state = {
      visible: [],
      visibleEdges: [],
      nodeEls: {},
      labelHalf: {},
      edgeEls: [],
      sim: {},
      adj: {},
      directed: {},
      byId: {},
      maxDegree: 1,
      colorMode: opts.colorMode || "community",
      onlyLinked: opts.onlyLinked !== undefined ? !!opts.onlyLinked : true,
      tagFilter: "",
      query: "",
      selectedId: opts.focus || null,
      hoveringId: null,
      pathMode: false,        // 路径模式：点两个节点看它们怎么连上的
      pathStart: null,
      pathEnd: null,
      pathNodes: {},          // id → "start" / "end" / "mid"
      pathEdges: {},          // "小id|大id" → true
      pathIds: [],
      pinned: null,          // 刚拖完的节点：松弛期间钉住，别被弹簧拽回原位
      fitPending: true,
      userMovedView: false
    };
    nodes.forEach(function (n) { state.byId[n.id] = n; });
    var positions = opts.persist === false ? {} : loadPositions();
    var params = loadParams();
    var restored = false;

    var view = { k: 1, tx: 0, ty: 0 };
    var alpha = 0;
    var rafId = null;
    var startTime = 0;
    var stopMs = STOP_MS;
    var drag = null;
    var pan = null;
    var pointers = {};
    var pinch = null;
    var linkDrag = null;        // Shift 拉线建联：{fromId, targetId, line}
    var linkDragEndedAt = 0;    // 拉线刚结束的时间戳：吃掉随后的 click（否则会跳去笔记页）

    var edgeLayer = document.createElementNS(SVGNS, "g");
    var nodeLayer = document.createElementNS(SVGNS, "g");
    var viewport = document.createElementNS(SVGNS, "g");
    setClass(viewport, "graph-viewport");
    viewport.appendChild(edgeLayer);
    viewport.appendChild(nodeLayer);

    /* ---------------- 尺寸 / 视图 ---------------- */
    function size() {
      var w = stage.clientWidth || 800;
      var h = stage.clientHeight || 520;
      return { w: w, h: h };
    }

    function applyView() {
      viewport.setAttribute(
        "transform",
        "translate(" + view.tx + "," + view.ty + ") scale(" + view.k + ")"
      );
    }

    function toGraph(clientX, clientY) {
      var rect = svg.getBoundingClientRect();
      return {
        x: (clientX - rect.left - view.tx) / view.k,
        y: (clientY - rect.top - view.ty) / view.k
      };
    }

    function zoomAt(clientX, clientY, factor) {
      var rect = svg.getBoundingClientRect();
      var px = clientX - rect.left;
      var py = clientY - rect.top;
      var next = clamp(view.k * factor, 0.25, 4);
      if (next === view.k) { return; }
      view.tx = px - (px - view.tx) * (next / view.k);
      view.ty = py - (py - view.ty) * (next / view.k);
      view.k = next;
      view.userMovedView = true;
      applyView();
    }

    function fit() {
      if (!state.visible.length) { return; }
      var dim = size();
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      state.visible.forEach(function (n) {
        var p = state.sim[n.id];
        if (!p) { return; }
        var r = radiusOf(n) + 26;   // 留出标题的余量
        minX = Math.min(minX, p.x - r);
        minY = Math.min(minY, p.y - r);
        maxX = Math.max(maxX, p.x + r);
        maxY = Math.max(maxY, p.y + r);
      });
      if (!isFinite(minX)) { return; }
      var pad = 16;
      var bw = Math.max(1, maxX - minX);
      var bh = Math.max(1, maxY - minY);
      var k = clamp(Math.min((dim.w - pad * 2) / bw, (dim.h - pad * 2) / bh), 0.3, 1.8);
      view.k = k;
      view.tx = dim.w / 2 - ((minX + maxX) / 2) * k;
      view.ty = dim.h / 2 - ((minY + maxY) / 2) * k;
      applyView();
    }

    /* ---------------- 节点半径（按连接数） ---------------- */
    function radiusOf(n) {
      if (n.focus) { return R_MAX + 3; }
      if (!n.has_links) { return R_MIN; }
      var ratio = Math.sqrt((n.degree || 0) / state.maxDegree);
      return R_MIN + (R_MAX - R_MIN) * ratio;
    }

    /* ---------------- 筛选 ---------------- */
    function computeVisible() {
      // 有搜索词时临时忽略「只看有链接」，否则孤立笔记里的命中项永远找不到
      var onlyLinked = state.onlyLinked && !state.query;
      var q = state.query.toLowerCase();
      state.visible = nodes.filter(function (n) {
        if (onlyLinked && !n.has_links) { return false; }
        if (state.tagFilter && (n.tags || []).indexOf(state.tagFilter) === -1) { return false; }
        return true;
      });
      var ids = {};
      state.visible.forEach(function (n) { ids[n.id] = 1; });
      state.visibleEdges = edges.filter(function (e) {
        return ids[e.source] && ids[e.target];
      });
      state.maxDegree = 1;
      state.visible.forEach(function (n) {
        if ((n.degree || 0) > state.maxDegree) { state.maxDegree = n.degree; }
      });
      // 无向邻接（高亮邻居用）
      state.adj = {};
      // 有向集合（判断「互链」用：A→B 与 B→A 同时存在才画成两条弧线）
      state.directed = {};
      state.visibleEdges.forEach(function (e) {
        (state.adj[e.source] = state.adj[e.source] || {})[e.target] = true;
        (state.adj[e.target] = state.adj[e.target] || {})[e.source] = true;
        (state.directed[e.source] = state.directed[e.source] || {})[e.target] = true;
      });
      state.matched = {};
      if (q) {
        state.visible.forEach(function (n) {
          if (String(n.title || "").toLowerCase().indexOf(q) !== -1) { state.matched[n.id] = true; }
        });
      }
    }

    /* ---------------- 坐标初始化 ---------------- */
    function ensureSim() {
      var dim = size();
      var span = Math.min(dim.w, dim.h) * 0.34;
      state.visible.forEach(function (n) {
        if (state.sim[n.id]) { return; }
        var saved = positions[n.id];
        if (saved && isFinite(saved[0]) && isFinite(saved[1])) {
          state.sim[n.id] = { x: saved[0], y: saved[1], vx: 0, vy: 0 };
          restored = true;
          return;
        }
        var ang = rand(0, Math.PI * 2);
        var rad = rand(0, span);
        state.sim[n.id] = {
          x: dim.w / 2 + Math.cos(ang) * rad,
          y: dim.h / 2 + Math.sin(ang) * rad,
          vx: 0, vy: 0
        };
      });
    }

    /* ---------------- 构建 SVG ---------------- */
    function build() {
      var dim = size();
      svg.setAttribute("width", dim.w);
      svg.setAttribute("height", dim.h);
      svg.setAttribute("viewBox", "0 0 " + dim.w + " " + dim.h);

      while (svg.firstChild) { svg.removeChild(svg.firstChild); }
      var defs = document.createElementNS(SVGNS, "defs");
      var marker = document.createElementNS(SVGNS, "marker");
      marker.setAttribute("id", (stage.id || "graph") + "-arrow");
      marker.setAttribute("viewBox", "0 0 10 10");
      marker.setAttribute("refX", "9");
      marker.setAttribute("refY", "5");
      marker.setAttribute("markerWidth", "7");
      marker.setAttribute("markerHeight", "7");
      marker.setAttribute("orient", "auto-start-reverse");
      var mPath = document.createElementNS(SVGNS, "path");
      mPath.setAttribute("d", "M0,0 L10,5 L0,10 z");
      setClass(mPath, "edge-arrow");
      marker.appendChild(mPath);
      defs.appendChild(marker);
      svg.appendChild(defs);
      svg.appendChild(viewport);

      edgeLayer.textContent = "";
      nodeLayer.textContent = "";
      state.edgeEls = [];
      state.visibleEdges.forEach(function (e) {
        var path = document.createElementNS(SVGNS, "path");
        setClass(path, "edge");
        path.setAttribute("marker-end", "url(#" + (stage.id || "graph") + "-arrow)");
        path.setAttribute("fill", "none");
        edgeLayer.appendChild(path);
        state.edgeEls.push({ path: path, source: e.source, target: e.target });
      });

      state.nodeEls = {};
      state.labelHalf = {};
      state.visible.forEach(function (n) {
        var g = document.createElementNS(SVGNS, "g");
        var cls = "node" + (n.has_links ? "" : " node--isolated");
        if (n.focus) { cls += " node--focus"; }
        setClass(g, cls);
        g.setAttribute("transform", "translate(0,0)");
        g.setAttribute("data-note-id", String(n.id));
        g.setAttribute("data-tip-title", n.title || "");
        var lines = [];
        if (n.degree) {
          lines.push(["连接", "入 " + (n.in_degree || 0) + " · 出 " + (n.out_degree || 0)]);
        }
        if ((n.tags || []).length) { lines.push(["标签", n.tags.join("、")]); }
        if (n.category) { lines.push(["分类", n.category]); }
        if (lines.length) {
          g.setAttribute("data-tip-lines", JSON.stringify(lines));
        }

        var hit = document.createElementNS(SVGNS, "circle");
        setClass(hit, "node__hit");

        var circle = document.createElementNS(SVGNS, "circle");
        var r = radiusOf(n);
        circle.setAttribute("r", String(r));
        setClass(circle, "node__circle");

        var label = document.createElementNS(SVGNS, "text");
        setClass(label, "node__label");
        label.setAttribute("text-anchor", "middle");
        label.textContent = n.title || "";

        g.appendChild(hit);
        g.appendChild(circle);
        g.appendChild(label);
        nodeLayer.appendChild(g);
        state.nodeEls[n.id] = { g: g, circle: circle, label: label, hit: hit, node: n, r: r };
        hit.setAttribute("r", String(r + 10));
        // 量一下标题实际宽度：标签重叠是关系图最常见的观感问题，
        // 让碰撞半径把它算进去（取半宽的一部分，够用又不至于把图撑得稀散）
        try {
          var bb = label.getBBox();
          state.labelHalf[n.id] = Math.min(46, bb.width / 2 * 0.6);
        } catch (e) {
          state.labelHalf[n.id] = 0;
        }
        bindNode(g, n);
        setClass(g, baseClass(n));
      });

      svg.setAttribute("class", "graph-svg" + (state.visible.length > 55 ? " graph-svg--dense" : ""));
      applyView();   // 即使还没缩放/平移，也把当前视图写进 transform（值可读、测试可断言）

      var empty = opts.emptyEl;
      if (empty) {
        if (state.visible.length === 0) { empty.classList.remove("is-hidden"); }
        else { empty.classList.add("is-hidden"); }
      }
    }

    /* ---------------- 着色 ---------------- */
    function colorIndexOf(n) {
      if (state.colorMode === "none" || !n.has_links) { return -1; }
      if (state.colorMode === "tag") { return typeof n.tag_color === "number" ? n.tag_color : -1; }
      if (state.colorMode === "category") {
        return typeof n.category_color === "number" ? n.category_color : -1;
      }
      return typeof n.community === "number" ? n.community : -1;
    }

    /* ---------------- 单步物理 ---------------- */
    function tick() {
      var list = state.visible;
      var n = list.length;
      var dim = size();
      var cx = dim.w / 2, cy = dim.h / 2;
      var i, j, a, b, p, q, dx, dy, d2, d;

      // 布局参数（面板可调）：一律相对基准值的百分比
      var repulsion = REPULSION * params.repulsion / 100;
      var springLen = SPRING_LEN * params.spring / 100;
      var centerK = CENTER_K * params.center / 100;

      // 斥力
      if (n < BH_THRESHOLD) {
        // 精确两两（小图保证布局质量）
        for (i = 0; i < n; i++) {
          a = list[i]; p = state.sim[a.id];
          for (j = i + 1; j < n; j++) {
            b = list[j]; q = state.sim[b.id];
            dx = p.x - q.x; dy = p.y - q.y;
            d2 = dx * dx + dy * dy;
            if (d2 < 1e-6) { dx = rand(-1, 1); dy = rand(-1, 1); d2 = 1; }
            d = Math.sqrt(d2);
            var f = repulsion / d2 * 0.5;
            var ux = dx / d, uy = dy / d;
            p.vx += ux * f; p.vy += uy * f;
            q.vx -= ux * f; q.vy -= uy * f;
          }
        }
      } else {
        // Barnes–Hut：先建四叉树，再逐点累计
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (i = 0; i < n; i++) {
          p = state.sim[list[i].id];
          if (p.x < minX) { minX = p.x; }
          if (p.x > maxX) { maxX = p.x; }
          if (p.y < minY) { minY = p.y; }
          if (p.y > maxY) { maxY = p.y; }
        }
        var half = Math.max(maxX - minX, maxY - minY, 1) / 2 + 1;
        var root = makeQuad((minX + maxX) / 2, (minY + maxY) / 2, half);
        var bodies = [];
        for (i = 0; i < n; i++) {
          var body = { id: list[i].id, x: state.sim[list[i].id].x, y: state.sim[list[i].id].y, mass: 1 };
          bodies.push(body);
          quadInsert(root, body);
        }
        for (i = 0; i < bodies.length; i++) {
          var force = { x: 0, y: 0 };
          quadForce(root, bodies[i], repulsion * 0.5, force);
          var sp = state.sim[bodies[i].id];
          sp.vx += force.x; sp.vy += force.y;
        }
      }

      // 弹簧（边）
      state.visibleEdges.forEach(function (e) {
        var ps = state.sim[e.source], pt = state.sim[e.target];
        if (!ps || !pt) { return; }
        dx = pt.x - ps.x; dy = pt.y - ps.y;
        d2 = dx * dx + dy * dy;
        if (d2 < 1e-6) { return; }
        d = Math.sqrt(d2);
        var f = SPRING_K * (d - springLen);
        ps.vx += dx / d * f; ps.vy += dy / d * f;
        pt.vx -= dx / d * f; pt.vy -= dy / d * f;
      });

      // 碰撞（避免节点重叠；借 d3-force 的 collide 思路，用邻近格加速）
      collide();

      // 居中 + 积分
      for (i = 0; i < n; i++) {
        a = list[i]; p = state.sim[a.id];
        p.vx += (cx - p.x) * centerK;
        p.vy += (cy - p.y) * centerK;
        if ((drag && drag.id === a.id) || state.pinned === a.id) {
          p.vx = 0; p.vy = 0; continue;
        }
        p.vx *= DAMPING; p.vy *= DAMPING;
        p.vx = clamp(p.vx, -MAX_V, MAX_V);
        p.vy = clamp(p.vy, -MAX_V, MAX_V);
        p.x += p.vx * alpha;
        p.y += p.vy * alpha;
        p.x = clamp(p.x, -HOME, HOME);
        p.y = clamp(p.y, -HOME, HOME);
      }
    }

    /* 碰撞：把节点按半径放进网格，只与邻近格比较 */
    function collide() {
      var cell = R_MAX * 2 + COLLIDE_PAD * 2;
      var grid = {};
      var list = state.visible;
      var i, key, bucket;
      for (i = 0; i < list.length; i++) {
        var n = list[i];
        var p = state.sim[n.id];
        key = Math.floor(p.x / cell) + ":" + Math.floor(p.y / cell);
        (grid[key] = grid[key] || []).push(n.id);
      }
      for (key in grid) {
        if (!Object.prototype.hasOwnProperty.call(grid, key)) { continue; }
        bucket = grid[key];
        var parts = key.split(":");
        var bx = parseInt(parts[0], 10), by = parseInt(parts[1], 10);
        for (var ox = -1; ox <= 1; ox++) {
          for (var oy = -1; oy <= 1; oy++) {
            var other = grid[(bx + ox) + ":" + (by + oy)];
            if (!other) { continue; }
            for (i = 0; i < bucket.length; i++) {
              for (var j = 0; j < other.length; j++) {
                if (other[j] <= bucket[i]) { continue; }
                separate(bucket[i], other[j]);
              }
            }
          }
        }
      }
    }

    // 正被拖 / 刚放下（钉住）的节点：碰撞时它不动，让邻居让开 ——
    // 否则「按住的那个」会被邻居挤走，「拖了等于没拖」。
    function heldId() {
      return drag ? drag.id : state.pinned;
    }

    function separate(idA, idB) {
      var a = state.byId[idA], b = state.byId[idB];
      var pa = state.sim[idA], pb = state.sim[idB];
      if (!a || !b || !pa || !pb) { return; }
      var labels = (state.labelHalf[a.id] || 0) + (state.labelHalf[b.id] || 0);
      var min = radiusOf(a) + radiusOf(b) + COLLIDE_PAD + labels;
      var dx = pb.x - pa.x, dy = pb.y - pa.y;
      var d2 = dx * dx + dy * dy;
      if (d2 >= min * min) { return; }
      var d = Math.sqrt(d2) || 0.01;
      var ux = dx / d, uy = dy / d;
      var held = heldId();
      var heldA = held === idA, heldB = held === idB;
      if (heldA && heldB) { return; }
      var pushA = (min - d) / 2, pushB = (min - d) / 2;
      if (heldA) { pushA = 0; pushB = min - d; }
      else if (heldB) { pushA = min - d; pushB = 0; }
      pa.x -= ux * pushA; pa.y -= uy * pushA;
      pb.x += ux * pushB; pb.y += uy * pushB;
    }

    /* ---------------- 渲染 ---------------- */
    function edgePath(e) {
      var sn = state.byId[e.source], tn = state.byId[e.target];
      var ps = state.sim[e.source], pt = state.sim[e.target];
      if (!sn || !tn || !ps || !pt) { return ""; }
      var dx = pt.x - ps.x, dy = pt.y - ps.y;
      var dist = Math.sqrt(dx * dx + dy * dy) || 1;
      var ux = dx / dist, uy = dy / dist;
      var rs = radiusOf(sn);
      var rt = radiusOf(tn) + 7;
      var x1 = ps.x + ux * rs, y1 = ps.y + uy * rs;
      var x2 = pt.x - ux * rt, y2 = pt.y - uy * rt;
      // 双向边：两条弧线朝相反方向弯开，不再重叠成一条
      var back = state.directed[e.target];
      var both = !!(back && back[e.source]);
      if (!both) { return "M" + x1 + " " + y1 + "L" + x2 + " " + y2; }
      var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      var off = Math.min(28, dist * 0.18);
      return "M" + x1 + " " + y1 + "Q" + (mx - uy * off) + " " + (my + ux * off) + " " + x2 + " " + y2;
    }

    function render() {
      state.edgeEls.forEach(function (e) {
        e.path.setAttribute("d", edgePath(e));
      });
      state.visible.forEach(function (n) {
        var p = state.sim[n.id];
        var els = state.nodeEls[n.id];
        if (!p || !els) { return; }
        els.g.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
        els.label.setAttribute("y", els.r + 13);
      });
    }

    /* ---------------- 模拟循环 ---------------- */
    function loop() {
      tick();
      render();
      alpha *= 0.985;
      if (alpha > 0.02 && (performance.now() - startTime) < stopMs) {
        rafId = requestAnimationFrame(loop);
      } else {
        rafId = null;
        state.pinned = null;          // 松弛结束：解除钉住
        if (state.fitPending && !state.userMovedView) { state.fitPending = false; fit(); }
        if (opts.persist !== false) { savePositions(state.sim); }
      }
    }

    function startSim(duration, initialAlpha) {
      stopMs = duration || STOP_MS;
      startTime = performance.now();
      if (initialAlpha !== undefined) {
        alpha = initialAlpha;
      } else {
        // 从记忆位置恢复时只做局部微调（别把用户熟悉的布局又揉一遍）
        alpha = restored && duration === undefined ? 0.35 : 1;
      }
      if (rafId) { cancelAnimationFrame(rafId); }
      rafId = requestAnimationFrame(loop);
    }

    /* ---------------- 高亮 ---------------- */
    function baseClass(n) {
      var cls = "node" + (n.has_links ? "" : " node--isolated");
      if (n.focus) { cls += " node--focus"; }
      var idx = colorIndexOf(n);
      if (idx >= 0) { cls += " gnode--c" + (idx % PALETTE); }
      if (state.query) {
        cls += state.matched && state.matched[n.id] ? " node--match" : " node--dim";
      }
      return cls;
    }

    function applyHighlight(focusId) {
      // 路径高亮优先：选好起终点后按路径显示、其余压暗（悬停仍会叠一层 hover）
      var pathOn = state.pathEnd !== null && state.pathEnd !== undefined;
      var near = {};
      if (!pathOn && focusId !== null && focusId !== undefined) {
        near[focusId] = true;
        var nbrs = state.adj[focusId] || {};
        for (var k in nbrs) {
          if (Object.prototype.hasOwnProperty.call(nbrs, k)) { near[k] = true; }
        }
      }
      state.visible.forEach(function (n) {
        var els = state.nodeEls[n.id];
        if (!els) { return; }
        var cls = baseClass(n);
        var mark = state.pathNodes[n.id];
        if (pathOn) {
          if (mark === "start") { cls += " node--path node--path-start"; }
          else if (mark === "end") { cls += " node--path node--path-end"; }
          else if (mark) { cls += " node--path node--path-mid"; }
          else { cls += " node--dim"; }
        } else if (mark) {
          cls += " node--path node--path-start";   // 只点了起点：标出来，但先不压暗别的
        } else if (focusId === null || focusId === undefined) {
          if (state.hoveringId === n.id) { cls += " node--hover"; }
        } else if (near[n.id]) {
          cls += n.id === focusId ? " node--selected" : " node--active";
        } else {
          cls += " node--dim";
        }
        setClass(els.g, cls);
      });
      state.edgeEls.forEach(function (e) {
        if (pathOn) {
          var onPath = state.pathEdges[edgeKey(e.source, e.target)];
          setClass(e.path, onPath ? "edge edge--path" : "edge edge--dim");
          return;
        }
        if (focusId === null || focusId === undefined) {
          setClass(e.path, "edge");
        } else if (String(e.source) === String(focusId) || String(e.target) === String(focusId)) {
          setClass(e.path, "edge edge--active");
        } else {
          setClass(e.path, "edge edge--dim");
        }
      });
    }

    /* ---------------- 最短路径 ----------------
     * 在**当前可见子图**上做 BFS（跳数最少）。无向邻接：双链在「怎么连上的」
     * 这个问题上是有向的，但用户想知道的通常是「这两篇有没有关系」，所以按无向走。 */
    function edgeKey(a, b) {
      var x = Number(a), y = Number(b);
      return x < y ? x + "|" + y : y + "|" + x;
    }

    function titleOf(id) {
      var n = state.byId[id];
      return n ? String(n.title || "") : "";
    }

    function bfsPath(from, to) {
      var start = String(from), goal = String(to);
      if (start === goal) { return [Number(from)]; }
      var prev = {}, seen = {}, queue = [start];
      seen[start] = true;
      while (queue.length) {
        var cur = queue.shift();
        var nbrs = state.adj[cur];
        if (!nbrs) { continue; }
        for (var key in nbrs) {
          if (!Object.prototype.hasOwnProperty.call(nbrs, key) || seen[key]) { continue; }
          seen[key] = true;
          prev[key] = cur;
          if (key === goal) {
            var chain = [goal], node = goal;
            while (prev[node] !== undefined) { node = prev[node]; chain.unshift(node); }
            return chain.map(Number);
          }
          queue.push(key);
        }
      }
      return null;
    }

    function recomputePath() {
      state.pathNodes = {};
      state.pathEdges = {};
      state.pathIds = [];
      if (state.pathStart !== null) { state.pathNodes[state.pathStart] = "start"; }
      if (state.pathStart === null || state.pathEnd === null) { return; }
      state.pathNodes[state.pathEnd] = "end";
      var chain = bfsPath(state.pathStart, state.pathEnd);
      if (!chain) { return; }        // 不可达：只留两个端点标记，边上全压暗
      state.pathIds = chain;
      chain.forEach(function (id, i) {
        state.pathNodes[id] = i === 0 ? "start" : (i === chain.length - 1 ? "end" : "mid");
      });
      for (var i = 0; i + 1 < chain.length; i++) {
        state.pathEdges[edgeKey(chain[i], chain[i + 1])] = true;
      }
    }

    function pathInfo() {
      return {
        mode: !!state.pathMode,
        start: state.pathStart,
        end: state.pathEnd,
        startTitle: state.pathStart === null ? "" : titleOf(state.pathStart),
        endTitle: state.pathEnd === null ? "" : titleOf(state.pathEnd),
        ids: state.pathIds.slice(),
        titles: state.pathIds.map(titleOf),
        hops: state.pathIds.length > 1 ? state.pathIds.length - 1 : 0,
        reachable: state.pathEnd === null ? null : state.pathIds.length > 0
      };
    }

    function notifyPath() {
      if (typeof opts.onPathChange === "function") {
        try { opts.onPathChange(pathInfo()); } catch (e) { /* 页面回调出错不影响引擎 */ }
      }
    }

    function pickPathNode(id) {
      if (state.pathStart === null || state.pathEnd !== null) {
        state.pathStart = id;      // 从头开始（或换一组）：这次点的当起点
        state.pathEnd = null;
      } else if (id === state.pathStart) {
        state.pathStart = null;    // 再点一次起点：取消
      } else {
        state.pathEnd = id;
      }
      recomputePath();
      refreshHighlight();
      notifyPath();
    }

    function clearPath(withMode) {
      state.pathStart = null;
      state.pathEnd = null;
      state.pathNodes = {};
      state.pathEdges = {};
      state.pathIds = [];
      if (withMode) { state.pathMode = false; }
      refreshHighlight();
      notifyPath();
    }

    function setPathMode(on) {
      state.pathMode = !!on;
      clearPath(false);
      return state.pathMode;
    }

    function refreshHighlight() {
      applyHighlight(state.selectedId !== null ? state.selectedId : null);
    }

    /* ---------------- 交互 ---------------- */
    function bindNode(g, n) {
      g.addEventListener("pointerenter", function () {
        state.hoveringId = n.id;
        applyHighlight(state.selectedId !== null ? state.selectedId : n.id);
        var els = state.nodeEls[n.id];
        if (els) {
          // 叠加在 applyHighlight 的结果之上（保留可能的 dim/active）
          setClass(els.g, (els.g.getAttribute("class") || "") + " node--hover");
        }
      });
      g.addEventListener("pointerleave", function () {
        state.hoveringId = null;
        refreshHighlight();
      });
      g.addEventListener("pointerdown", function (ev) {
        if (ev.button !== undefined && ev.button !== 0) { return; }
        ev.stopPropagation();
        if (ev.shiftKey) {
          // Shift + 按下：从这个节点拉一条虚线，拖到目标节点上松手 → 确认建联
          linkDrag = { fromId: n.id, targetId: null, line: null };
          var lstart = state.sim[n.id];
          if (lstart) {
            linkDrag.line = document.createElementNS(SVGNS, "line");
            setClass(linkDrag.line, "graph-link-draw");
            linkDrag.line.setAttribute("x1", lstart.x);
            linkDrag.line.setAttribute("y1", lstart.y);
            linkDrag.line.setAttribute("x2", lstart.x);
            linkDrag.line.setAttribute("y2", lstart.y);
            edgeLayer.appendChild(linkDrag.line);
          }
          ev.preventDefault();
          return;
        }
        var p = state.sim[n.id];
        drag = {
          id: n.id, moved: false,
          sx: ev.clientX, sy: ev.clientY,
          offX: 0, offY: 0, front: false
        };
        if (p) {
          var gp = toGraph(ev.clientX, ev.clientY);
          drag.offX = p.x - gp.x;
          drag.offY = p.y - gp.y;
        }
        // 不用 setPointerCapture：捕获后 click 会派发到 <svg> 而不是节点，
        // 节点上的 click 就永远不触发了。改为把 pointermove 挂到 document。
        ev.preventDefault();
      });
      g.addEventListener("click", function (ev) {
        if (Date.now() - linkDragEndedAt < 500) { ev.preventDefault(); return; }
        if (drag && drag.moved) { ev.preventDefault(); return; }
        if (state.pathMode) {
          // 路径模式：点节点 = 选起终点（打开笔记要先退出路径模式）
          ev.preventDefault();
          pickPathNode(n.id);
          return;
        }
        if (opts.onOpen) { opts.onOpen(n); return; }
        window.location.href = "/notes/" + n.id;
      });
    }

    function onPointerMove(ev) {
      if (pinch) { handlePinch(ev); return; }
      if (linkDrag) {
        var from = state.sim[linkDrag.fromId];
        if (!from) { return; }
        var gl = toGraph(ev.clientX, ev.clientY);
        if (linkDrag.line) {
          linkDrag.line.setAttribute("x2", gl.x);
          linkDrag.line.setAttribute("y2", gl.y);
        }
        // 命中最近的其它节点（图坐标阈值 30，与缩放无关）
        var best = null, bestD = 30 * 30;
        for (var nid in state.sim) {
          if (String(nid) === String(linkDrag.fromId)) { continue; }
          var q = state.sim[nid];
          var dd = (q.x - gl.x) * (q.x - gl.x) + (q.y - gl.y) * (q.y - gl.y);
          if (dd < bestD) { bestD = dd; best = nid; }
        }
        linkDrag.targetId = best == null ? null : parseInt(best, 10);
        return;
      }
      if (drag) {
        var p = state.sim[drag.id];
        if (!p) { return; }
        var gp = toGraph(ev.clientX, ev.clientY);
        if (Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) > 3) { drag.moved = true; }
        p.x = clamp(gp.x + drag.offX, -HOME, HOME);
        p.y = clamp(gp.y + drag.offY, -HOME, HOME);
        p.vx = 0; p.vy = 0;
        var els = state.nodeEls[drag.id];
        if (!drag.front && els && els.g.parentNode) {
          els.g.parentNode.appendChild(els.g);
          drag.front = true;
        }
        render();
        return;
      }
      if (pan) {
        view.tx += ev.clientX - pan.x;
        view.ty += ev.clientY - pan.y;
        pan.x = ev.clientX; pan.y = ev.clientY;
        view.userMovedView = true;
        applyView();
      }
    }

    function onPointerUp() {
      pointers = {};
      pinch = null;
      if (linkDrag) {
        var ld = linkDrag;
        linkDrag = null;
        linkDragEndedAt = Date.now();
        if (ld.line && ld.line.parentNode) { ld.line.parentNode.removeChild(ld.line); }
        if (ld.targetId != null && opts.onLinkAsk) {
          var na = state.byId[ld.fromId], nb = state.byId[ld.targetId];
          if (na && nb) { opts.onLinkAsk(na, nb); }
        }
        return;
      }
      if (drag) {
        var wasMoved = drag.moved;
        var movedId = drag.id;
        drag = null;
        if (wasMoved && opts.persist !== false) { savePositions(state.sim); }
        if (wasMoved) {
          // 钉住刚拖完的节点，让邻居轻轻让开——否则弹簧会立刻把它拽回原位，
          // 那「拖了等于没拖」。松弛结束（loop 停止）时自动解除。
          state.pinned = movedId;
          startSim(800, 0.2);
        }
      }
      pan = null;
    }

    function handlePinch(ev) {
      var ids = Object.keys(pointers);
      if (ids.length < 2) { return; }
      var a = pointers[ids[0]], b = pointers[ids[1]];
      var dist = Math.hypot(a.x - b.x, a.y - b.y);
      var midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2;
      if (pinch.last) {
        zoomAt(midX, midY, dist / (pinch.last || dist));
        // 双指整体移动 → 平移（和地图一致）
        if (pinch.mid) {
          view.tx += midX - pinch.mid[0];
          view.ty += midY - pinch.mid[1];
          view.userMovedView = true;
          applyView();
        }
      }
      pinch.last = dist;
      pinch.mid = [midX, midY];
      void ev;
    }

    stage.addEventListener("wheel", function (ev) {
      if (opts.wheelZoom === false && !ev.ctrlKey && !ev.metaKey) { return; }
      ev.preventDefault();
      zoomAt(ev.clientX, ev.clientY, Math.exp(-ev.deltaY * 0.0016));
    }, { passive: false });

    stage.addEventListener("pointerdown", function (ev) {
      pointers[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
      var ids = Object.keys(pointers);
      if (ids.length === 2) {
        pinch = { last: 0, mid: null };
        pan = null;
        if (ev.preventDefault) { ev.preventDefault(); }
        return;
      }
      // 不在节点上（空白、边、画布）→ 平移画布。
      // 触摸时**不用单指平移**：那会把页面滚动吃掉（手机上最烦的就是这个），
      // 单指留给页面滚动，图谱用双指拖/捏（见 handlePinch）。
      var onNode = ev.target && ev.target.closest && ev.target.closest(".node");
      var touch = ev.pointerType && ev.pointerType !== "mouse" && ev.pointerType !== "pen";
      if (!onNode && !touch) {
        pan = { x: ev.clientX, y: ev.clientY };
        view.userMovedView = true;
        ev.preventDefault();
      }
    });

    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
    stage.addEventListener("pointerleave", function (ev) {
      delete pointers[ev.pointerId];
    });

    // 键盘：方向键选点、Enter 打开、+/- 缩放、0 适应
    stage.addEventListener("keydown", function (ev) {
      var handled = true;
      switch (ev.key) {
        case "ArrowRight": moveSelection(1, 0); break;
        case "ArrowLeft": moveSelection(-1, 0); break;
        case "ArrowDown": moveSelection(0, 1); break;
        case "ArrowUp": moveSelection(0, -1); break;
        case "+": case "=": zoomAt(centreOfView().x, centreOfView().y, 1.25); break;
        case "-": case "_": zoomAt(centreOfView().x, centreOfView().y, 0.8); break;
        case "0": fit(); break;
        case "Enter":
          if (state.selectedId !== null && state.pathMode) {
            pickPathNode(state.selectedId);   // 路径模式下回车 = 把当前高亮节点设为端点
          } else if (state.selectedId !== null) {
            if (opts.onOpen) { opts.onOpen(state.byId[state.selectedId]); }
            else { window.location.href = "/notes/" + state.selectedId; }
          }
          break;
        case "Escape":
          if (state.pathMode) { setPathMode(false); } else { handled = false; }
          break;
        default: handled = false;
      }
      if (handled) { ev.preventDefault(); }
    });

    function centreOfView() {
      var rect = svg.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }

    function selectById(id, withFocus) {
      state.selectedId = id;
      applyHighlight(id);
      var els = state.nodeEls[id];
      if (els) {
        try { els.g.focus(); } catch (e) { /* 某些浏览器对 SVG focus 有限制 */ }
      }
      if (withFocus) { centreOn(id); }
    }

    function centreOn(id) {
      var p = state.sim[id];
      if (!p) { return; }
      var dim = size();
      view.tx = dim.w / 2 - p.x * view.k;
      view.ty = dim.h / 2 - p.y * view.k;
      view.userMovedView = true;
      applyView();
    }

    function moveSelection(dx, dy) {
      var ids = state.visible.map(function (n) { return n.id; });
      if (!ids.length) { return; }
      if (state.selectedId === null || !state.sim[state.selectedId]) {
        selectById(ids[0], true);
        return;
      }
      var cur = state.sim[state.selectedId];
      var best = null, bestScore = -Infinity;
      state.visible.forEach(function (n) {
        if (n.id === state.selectedId) { return; }
        var p = state.sim[n.id];
        if (!p) { return; }
        var vx = p.x - cur.x, vy = p.y - cur.y;
        var d = Math.sqrt(vx * vx + vy * vy) || 1;
        var dot = (vx * dx + vy * dy) / d;
        if (dot <= 0.15) { return; }
        var score = dot - d / Math.max(size().w, size().h);
        if (score > bestScore) { bestScore = score; best = n.id; }
      });
      if (best === null) {
        var nd = Infinity;
        state.visible.forEach(function (n) {
          if (n.id === state.selectedId) { return; }
          var p = state.sim[n.id];
          if (!p) { return; }
          var d2 = (p.x - cur.x) * (p.x - cur.x) + (p.y - cur.y) * (p.y - cur.y);
          if (d2 < nd) { nd = d2; best = n.id; }
        });
      }
      if (best !== null) { selectById(best, true); }
    }

    /* ---------------- 布局参数 ---------------- */
    function setParams(next, options) {
      var run = !(options && options.silent);
      var changed = false;
      PARAM_KEYS.forEach(function (k) {
        if (!next || next[k] === undefined || next[k] === null) { return; }
        var v = Number(next[k]);
        if (!isFinite(v)) { return; }
        v = clamp(Math.round(v), PARAM_LIMITS[k][0], PARAM_LIMITS[k][1]);
        if (v !== params[k]) { params[k] = v; changed = true; }
      });
      if (changed) {
        if (opts.persist !== false) { schedulePersist(params); }
        // 中等强度跑一段：立刻能看出松紧变化，又不会把用户熟悉的布局整片揉散
        if (run) { startSim(1200, 0.5); }
      }
      return getParams();
    }

    function getParams() {
      return { repulsion: params.repulsion, spring: params.spring, center: params.center };
    }

    function resetParams(options) {
      PARAM_KEYS.forEach(function (k) { params[k] = PARAM_DEFAULTS[k]; });
      if (opts.persist !== false) { schedulePersist(params); }
      if (!(options && options.silent)) { startSim(1200, 0.5); }
      return getParams();
    }

    /* ---------------- 重排 / 视图 ---------------- */
    function relayout() {
      state.sim = {};
      state.pinned = null;
      restored = false;
      state.fitPending = true;
      state.userMovedView = false;
      ensureSim();
      state.visible.forEach(function (n) {
        var p = state.sim[n.id];
        if (!p) { return; }
        // 重新随机撒开，避免从旧位置“懒得动”
        var dim = size();
        var ang = rand(0, Math.PI * 2), rad = rand(0, Math.min(dim.w, dim.h) * 0.34);
        p.x = dim.w / 2 + Math.cos(ang) * rad;
        p.y = dim.h / 2 + Math.sin(ang) * rad;
        p.vx = 0; p.vy = 0;
        delete positions[n.id];   // 只丢弃这些节点记住的位置
      });
      startSim(STOP_MS);
    }

    function setColorMode(mode) {
      state.colorMode = mode;
      state.visible.forEach(function (n) {
        var els = state.nodeEls[n.id];
        if (els) { setClass(els.g, baseClass(n)); }
      });
      refreshHighlight();
      renderLegend();
    }

    function setQuery(q) {
      state.query = String(q || "").trim();
      computeVisible();
      ensureSim();
      build();
      render();
      refreshHighlight();
      renderLegend();
      startSim(500);
      return Object.keys(state.matched || {}).length;
    }

    function setFilter(next) {
      if (next.onlyLinked !== undefined) { state.onlyLinked = !!next.onlyLinked; }
      if (next.tag !== undefined) { state.tagFilter = next.tag || ""; }
      computeVisible();
      ensureSim();
      build();
      render();
      refreshHighlight();
      renderLegend();
      startSim(900);
    }

    /* ---------------- 图例 ---------------- */
    function renderLegend() {
      var el = opts.legendEl;
      if (!el) { return; }
      el.textContent = "";
      var groups = {};
      var order = [];
      if (state.colorMode === "none") {
        push(".", "全部节点", null);
      } else if (state.colorMode === "tag") {
        state.visible.forEach(function (n) {
          if (!n.has_links) { return; }
          var idx = typeof n.tag_color === "number" ? n.tag_color : -1;
          if (idx < 0) { return; }
          push(String(idx), n.tags && n.tags[0] ? n.tags[0] : "（无标签）", idx % PALETTE);
        });
      } else if (state.colorMode === "category") {
        state.visible.forEach(function (n) {
          if (!n.has_links) { return; }
          var idx = typeof n.category_color === "number" ? n.category_color : -1;
          if (idx < 0) { return; }
          push(String(idx), n.category || "（未分类）", idx % PALETTE);
        });
      } else {
        state.visible.forEach(function (n) {
          var idx = typeof n.community === "number" ? n.community : -1;
          if (idx < 0) { return; }
          push(String(idx), "聚类 " + (idx + 1), idx % PALETTE);
        });
      }
      function push(key, label, color) {
        if (!groups[key]) {
          groups[key] = { label: label, color: color, n: 0 };
          order.push(key);
        }
        groups[key].n += 1;
      }
      order.sort(function (a, b) { return groups[b].n - groups[a].n; });
      var limited = order.slice(0, 8);
      limited.forEach(function (key) {
        var info = groups[key];
        var row = document.createElement("div");
        row.className = "graph-legend__row";
        var swatch = document.createElement("span");
        if (info.color === null || info.color === undefined) {
          swatch.className = "graph-legend__swatch graph-legend__swatch--plain";
        } else {
          swatch.className = "graph-legend__swatch gswatch--c" + info.color;
        }
        var label = document.createElement("span");
        label.className = "graph-legend__label";
        label.textContent = info.label;
        var count = document.createElement("span");
        count.className = "graph-legend__count";
        count.textContent = String(info.n);
        row.appendChild(swatch);
        row.appendChild(label);
        row.appendChild(count);
        el.appendChild(row);
      });
      if (order.length > limited.length) {
        var more = document.createElement("p");
        more.className = "graph-legend__note";
        more.textContent = "另有 " + (order.length - limited.length) + " 组未列出";
        el.appendChild(more);
      }
    }

    /* ---------------- 导出图片 ----------------
     * 页面样式都在样式表里，序列化出去的 SVG 带不走 —— 所以先把**计算样式内联**
     * 到克隆副本上（fill / stroke / font-* / opacity…），导出的图在哪儿都长一样。
     * 导出的是「整张图」（与当前缩放平移无关），按内容 bbox 裁边并留白。 */
    var EXPORT_KEYS = [
      "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
      "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "opacity",
      "font-family", "font-size", "font-weight", "font-style", "letter-spacing",
      "text-anchor", "dominant-baseline", "paint-order", "shape-rendering"
    ];

    function inlineComputedStyles(srcRoot, dstRoot) {
      var from = [srcRoot].concat(Array.prototype.slice.call(srcRoot.querySelectorAll("*")));
      var to = [dstRoot].concat(Array.prototype.slice.call(dstRoot.querySelectorAll("*")));
      var n = Math.min(from.length, to.length);
      for (var i = 0; i < n; i++) {
        var cs = window.getComputedStyle(from[i]);
        if (!cs) { continue; }
        for (var j = 0; j < EXPORT_KEYS.length; j++) {
          var value = cs.getPropertyValue(EXPORT_KEYS[j]);
          if (value) { to[i].setAttribute(EXPORT_KEYS[j], value); }
        }
        to[i].removeAttribute("class");
      }
    }

    function pageBackground() {
      var node = stage;
      while (node && node.nodeType === 1) {
        var bg = window.getComputedStyle(node).backgroundColor;
        if (bg && bg !== "transparent" && bg.indexOf("rgba(0, 0, 0, 0)") !== 0) { return bg; }
        node = node.parentNode;
      }
      var token = window.getComputedStyle(document.documentElement).getPropertyValue("--surface");
      return (token || "").trim() || "#ffffff";
    }

    function exportStamp() {
      var d = new Date();
      function p2(x) { return (x < 10 ? "0" : "") + x; }
      return d.getFullYear() + p2(d.getMonth() + 1) + p2(d.getDate())
        + "-" + p2(d.getHours()) + p2(d.getMinutes());
    }

    function buildExportSVG(scale) {
      var factor = scale || 1;
      var pad = 28;
      var box = null;
      try { box = viewport.getBBox(); } catch (e) { box = null; }
      if (!box || !isFinite(box.width) || !isFinite(box.height) || box.width <= 0 || box.height <= 0) {
        var fallback = size();
        box = { x: 0, y: 0, width: fallback.w, height: fallback.h };
      }
      var w = Math.ceil(box.width + pad * 2);
      var h = Math.ceil(box.height + pad * 2);
      var clone = svg.cloneNode(true);
      // 先取到视图组再内联样式（内联会把 class 去掉，之后就找不着了）
      var vp = clone.querySelector(".graph-viewport");
      if (vp) { vp.removeAttribute("transform"); }
      inlineComputedStyles(svg, clone);
      clone.setAttribute("xmlns", SVGNS);
      clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
      clone.setAttribute("viewBox", (box.x - pad) + " " + (box.y - pad) + " " + w + " " + h);
      clone.setAttribute("width", Math.round(w * factor));
      clone.setAttribute("height", Math.round(h * factor));
      clone.removeAttribute("style");
      // 背景：导出的图不能是透明的（贴到白底文档里节点就看不见了）
      var bg = document.createElementNS(SVGNS, "rect");
      bg.setAttribute("x", String(box.x - pad));
      bg.setAttribute("y", String(box.y - pad));
      bg.setAttribute("width", String(w));
      bg.setAttribute("height", String(h));
      bg.setAttribute("fill", pageBackground());
      clone.insertBefore(bg, clone.firstChild);
      return {
        markup: new XMLSerializer().serializeToString(clone),
        width: Math.round(w * factor),
        height: Math.round(h * factor)
      };
    }

    function downloadURL(url, name) {
      var a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.setTimeout(function () { try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ } }, 8000);
    }

    function exportImage(kind) {
      var isPng = kind === "png";
      var info = buildExportSVG(isPng ? 2 : 1);
      var name = (opts.exportName || "inknote-graph") + "-" + exportStamp() + (isPng ? ".png" : ".svg");
      if (!isPng) {
        downloadURL(URL.createObjectURL(new Blob([info.markup], { type: "image/svg+xml;charset=utf-8" })), name);
        return Promise.resolve({ kind: "svg", name: name, width: info.width, height: info.height });
      }
      return new Promise(function (resolve, reject) {
        var img = new Image();
        img.onload = function () {
          try {
            var canvas = document.createElement("canvas");
            canvas.width = info.width;
            canvas.height = info.height;
            var ctx = canvas.getContext("2d");
            ctx.fillStyle = pageBackground();
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(function (blob) {
              if (!blob) { reject(new Error("PNG 生成失败")); return; }
              downloadURL(URL.createObjectURL(blob), name);
              resolve({ kind: "png", name: name, width: canvas.width, height: canvas.height });
            }, "image/png");
          } catch (e) { reject(e); }
        };
        img.onerror = function () { reject(new Error("图片渲染失败")); };
        img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(info.markup);
      });
    }

    /* ---------------- 在图上直接连两篇 ----------------
     * 服务端往正文末尾追加 [[标题]] 之后，本地把这条边补进图里：
     * 不用刷新就能看到新连线（刷新后仍以服务端数据为准，两边一致）。 */
    function bumpDegree(n, side) {
      if (side === "out") { n.out_degree = (n.out_degree || 0) + 1; }
      else { n.in_degree = (n.in_degree || 0) + 1; }
      n.degree = (n.degree || 0) + 1;
      n.has_links = true;
    }

    function addLink(sourceId, targetId) {
      var s = Number(sourceId), t = Number(targetId);
      var sn = state.byId[s], tn = state.byId[t];
      if (!sn || !tn) { return false; }
      var exists = edges.some(function (e) {
        return Number(e.source) === s && Number(e.target) === t;
      });
      if (exists) { return false; }
      edges.push({ source: s, target: t, title: titleOf(t) });
      bumpDegree(sn, "out");
      bumpDegree(tn, "in");
      computeVisible();
      recomputePath();      // 刚建好的这条线要是正好在查的路径上，立刻点亮
      ensureSim();
      build();
      render();
      refreshHighlight();
      renderLegend();
      notifyPath();
      startSim(900, 0.45);
      return true;
    }

    /* ---------------- 启动 ---------------- */
    function init() {
      computeVisible();
      ensureSim();
      build();
      render();
      var focusVisible = opts.focus && state.visible.some(function (n) {
        return n.id === opts.focus;
      });
      if (focusVisible) {
        // 从笔记页跳过来：把焦点节点居中选中
        var p = state.sim[opts.focus];
        if (p) {
          var dim = size();
          view.k = 1;
          view.tx = dim.w / 2 - p.x;
          view.ty = dim.h / 2 - p.y;
          view.userMovedView = true;
          applyView();
        }
        selectById(opts.focus, true);
      } else if (opts.focus) {
        state.selectedId = null;   // 焦点节点不在当前视图里：不选中，避免整图变暗
      }
      startSim();
      renderLegend();
    }

    init();

    return {
      stage: stage,
      state: state,
      fit: fit,
      relayout: relayout,
      setColorMode: setColorMode,
      setParams: setParams,
      getParams: getParams,
      resetParams: resetParams,
      setQuery: setQuery,
      setFilter: setFilter,
      selectById: selectById,
      setPathMode: setPathMode,
      pickPathNode: pickPathNode,
      clearPath: clearPath,
      pathInfo: pathInfo,
      addLink: addLink,
      exportImage: exportImage,
      _path: bfsPath,
      _exportSVG: buildExportSVG,
      zoomAt: zoomAt,
      _edgePath: edgePath,
      _tick: tick,
      _radiusOf: radiusOf,
      _quad: { makeQuad: makeQuad, insert: quadInsert, force: quadForce }
    };
  }

  /* ============================================ 页面接线 */
  function initGlobal() {
    var stage = document.getElementById("graph-stage");
    if (!stage) { return; }
    var payload = readPayload(document.getElementById("graph-data"));
    if (!payload) { return; }
    var fallback = document.getElementById("graph-fallback");
    if (fallback) { fallback.classList.add("is-hidden"); }

    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("id", "graph-svg");      // 保持既有选择器契约
    setClass(svg, "graph-svg");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    stage.appendChild(svg);

    var focusId = stage.getAttribute("data-focus");
    var view = create(stage, payload, {
      focus: focusId ? parseInt(focusId, 10) : null,
      legendEl: document.getElementById("graph-legend"),
      emptyEl: document.getElementById("graph-empty"),
      colorMode: readSelect("graph-color", "community"),
      onlyLinked: true,
      exportName: "inknote-graph",
      onLinkAsk: showLinkAsk,
      onPathChange: function (info) { onPath(info); }
    });
    if (!view) { return; }

    var searchEl = document.getElementById("graph-search");
    var matchEl = document.getElementById("graph-match");
    var onlyLinkedEl = document.getElementById("graph-only-linked");
    var tagEl = document.getElementById("graph-tag");
    var colorEl = document.getElementById("graph-color");

    if (searchEl) {
      var timer = null;
      searchEl.addEventListener("input", function () {
        if (timer) { clearTimeout(timer); }
        timer = setTimeout(function () {
          var hits = view.setQuery(searchEl.value);
          if (matchEl) {
            matchEl.textContent = searchEl.value.trim()
              ? (hits ? "命中 " + hits + " 篇（回车打开第一个）" : "没有匹配的笔记")
              : "";
          }
        }, 130);
      });
      searchEl.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") { return; }
        var matched = Object.keys(view.state.matched || {});
        if (!matched.length) { return; }
        ev.preventDefault();
        var first = parseInt(matched[0], 10);
        view.selectById(first, true);
      });
    }
    if (onlyLinkedEl) {
      onlyLinkedEl.addEventListener("change", function () {
        view.setFilter({ onlyLinked: onlyLinkedEl.checked });
      });
    }
    if (tagEl) {
      tagEl.addEventListener("change", function () {
        view.setFilter({ tag: tagEl.value });
      });
    }
    if (colorEl) {
      colorEl.addEventListener("change", function () {
        view.setColorMode(colorEl.value);
      });
    }
    bindButton("graph-zoom-in", function () { view.zoomAt(0, 0, 1.25); });
    bindButton("graph-zoom-out", function () { view.zoomAt(0, 0, 0.8); });
    bindButton("graph-fit", function () { view.fit(); });
    bindButton("graph-relayout", function () { view.relayout(); });

    /* ---------- 路径模式：选两篇看它们怎么连上的 ---------- */
    var pathBtn = document.getElementById("graph-path");
    var pathResult = document.getElementById("graph-path-result");
    var pathActions = document.getElementById("graph-path-actions");
    var linkFwd = document.getElementById("graph-link-fwd");
    var linkRev = document.getElementById("graph-link-rev");
    var hintEl = document.querySelector(".graph-toolbar__hint");
    var HINT_TEXT = hintEl ? hintEl.innerHTML : "";
    var PATH_HINT = "路径模式：点两个节点（键盘：方向键选 + 回车）看最短路径，Esc 退出";

    function cutTitle(text) {
      var s = String(text || "");
      return s.length > 10 ? s.slice(0, 9) + "…" : s;
    }

    function linkLabel(btn, from, to) {
      if (!btn) { return; }
      btn.textContent = cutTitle(from) + " → " + cutTitle(to);
      btn.title = "在《" + from + "》的正文末尾加上指向《" + to + "》的链接（可在这篇的历史版本里撤销）";
      btn.setAttribute("aria-label", btn.title);
    }

    function onPath(info) {
      if (pathBtn) { pathBtn.setAttribute("aria-pressed", info.mode ? "true" : "false"); }
      if (hintEl) { hintEl.innerHTML = info.mode ? PATH_HINT : HINT_TEXT; }
      if (pathResult) {
        if (!info.mode) {
          pathResult.textContent = "";
        } else if (info.start === null) {
          pathResult.textContent = "路径模式：随便点一篇作为起点。";
        } else if (info.end === null) {
          pathResult.textContent = "起点：《" + info.startTitle + "》—— 再点一篇作为终点。";
        } else if (!info.reachable) {
          pathResult.textContent = "《" + info.startTitle + "》和《" + info.endTitle
            + "》在当前视图里没有连通的路径（也许要先取消筛选）。";
        } else {
          pathResult.textContent = "最短路径 " + info.hops + " 步：" + info.titles.map(function (t) {
            return "《" + t + "》";
          }).join(" → ");
        }
      }
      var both = info.mode && info.start !== null && info.end !== null;
      if (pathActions) { pathActions.classList.toggle("is-hidden", !both); }
      if (both) {
        linkLabel(linkFwd, info.startTitle, info.endTitle);
        linkLabel(linkRev, info.endTitle, info.startTitle);
      }
    }

    var linking = false;
    function makeLink(from, to) {
      var api = window.InkNote;
      if (linking || !api || !api.fetchJSON) { return; }
      linking = true;
      if (linkFwd) { linkFwd.disabled = true; }
      if (linkRev) { linkRev.disabled = true; }
      api.fetchJSON("/notes/" + from + "/link.json", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "target_id=" + encodeURIComponent(String(to))
      }).then(function (data) {
        // 本地把新边补进图里：不用刷新就能看到连线
        var added = view.addLink(from, to);
        if (api.toast) {
          api.toast((data && data.message) || (added ? "已建立链接" : "这两篇已经连着了"));
        }
      }).catch(function (err) {
        if (api.toast) { api.toast((err && err.message) || "建立链接失败", "error"); }
      }).then(function () {
        linking = false;
        if (linkFwd) { linkFwd.disabled = false; }
        if (linkRev) { linkRev.disabled = false; }
      });
    }

    /* ---- Shift 拉线建联：确认条 ---- */
    var askBar = document.getElementById("graph-link-ask");
    var askFwd = document.getElementById("graph-link-ask-fwd");
    var askRev = document.getElementById("graph-link-ask-rev");
    var askCancel = document.getElementById("graph-link-ask-cancel");
    var askFrom = null, askTo = null;

    function showLinkAsk(a, b) {
      askFrom = a; askTo = b;
      if (askFwd) { askFwd.textContent = "「" + a.title + "」→「" + b.title + "」"; }
      if (askRev) { askRev.textContent = "「" + b.title + "」→「" + a.title + "」"; }
      if (askBar) { askBar.classList.remove("is-hidden"); }
    }
    function hideLinkAsk() {
      if (askBar) { askBar.classList.add("is-hidden"); }
      askFrom = null; askTo = null;
    }
    if (askFwd) {
      askFwd.addEventListener("click", function () {
        if (askFrom && askTo) { makeLink(askFrom.id, askTo.id); hideLinkAsk(); }
      });
    }
    if (askRev) {
      askRev.addEventListener("click", function () {
        if (askFrom && askTo) { makeLink(askTo.id, askFrom.id); hideLinkAsk(); }
      });
    }
    if (askCancel) { askCancel.addEventListener("click", hideLinkAsk); }

    if (pathBtn) {
      pathBtn.addEventListener("click", function (ev) {
        ev.preventDefault();
        view.setPathMode(pathBtn.getAttribute("aria-pressed") !== "true");
      });
    }
    if (linkFwd) {
      linkFwd.addEventListener("click", function () {
        var p = view.pathInfo();
        if (p.start !== null && p.end !== null) { makeLink(p.start, p.end); }
      });
    }
    if (linkRev) {
      linkRev.addEventListener("click", function () {
        var p = view.pathInfo();
        if (p.start !== null && p.end !== null) { makeLink(p.end, p.start); }
      });
    }
    bindButton("graph-path-clear", function () { view.clearPath(false); });
    onPath(view.pathInfo());

    /* ---------- 导出图片 ---------- */
    var exporting = false;
    function runExport(kind) {
      if (exporting) { return; }
      exporting = true;
      Promise.resolve(view.exportImage(kind)).then(function (info) {
        if (window.InkNote && window.InkNote.toast) {
          window.InkNote.toast("已导出" + info.name + "（" + info.width + "×" + info.height + "）");
        }
      }).catch(function (err) {
        if (window.InkNote && window.InkNote.toast) {
          window.InkNote.toast((err && err.message) || "导出失败", "error");
        }
      }).then(function () { exporting = false; });
    }
    bindButton("graph-export-png", function () { runExport("png"); });
    bindButton("graph-export-svg", function () { runExport("svg"); });

    /* ---------- 布局参数（松紧） ---------- */
    var PARAM_INPUTS = [
      ["repulsion", "graph-p-repulsion"],
      ["spring", "graph-p-spring"],
      ["center", "graph-p-center"]
    ];

    function syncParamInputs() {
      var now = view.getParams();
      PARAM_INPUTS.forEach(function (pair) {
        var input = document.getElementById(pair[1]);
        var label = document.getElementById(pair[1] + "-v");
        if (input) { input.value = String(now[pair[0]]); }
        if (label) { label.textContent = now[pair[0]] + "%"; }
      });
    }

    PARAM_INPUTS.forEach(function (pair) {
      var input = document.getElementById(pair[1]);
      if (!input) { return; }
      input.addEventListener("input", function () {
        var next = {};
        next[pair[0]] = Number(input.value);
        view.setParams(next);      // 边拖边生效：立刻能看出疏密变化
        var label = document.getElementById(pair[1] + "-v");
        if (label) { label.textContent = input.value + "%"; }
      });
    });
    bindButton("graph-p-reset", function () {
      view.resetParams();
      syncParamInputs();
    });
    syncParamInputs();
  }

  function readSelect(id, fallback) {
    var el = document.getElementById(id);
    return el && el.value ? el.value : fallback;
  }

  function bindButton(id, fn) {
    var el = document.getElementById(id);
    if (!el) { return; }
    el.addEventListener("click", function (ev) {
      ev.preventDefault();
      fn();
    });
  }

  function initLocal() {
    var stage = document.getElementById("local-graph-stage");
    if (!stage) { return; }
    var payload = readPayload(document.getElementById("local-graph-data"));
    if (!payload || !(payload.nodes || []).length) { return; }
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("id", "local-graph-svg");
    setClass(svg, "graph-svg");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    stage.appendChild(svg);
    create(stage, payload, {
      focus: payload.focus || null,
      legendEl: null,
      colorMode: "tag",
      onlyLinked: false,
      wheelZoom: false,      // 局部图很小，滚轮留给页面滚动
      persist: true
    });
  }

  function boot() {
    try { initGlobal(); } catch (e) { /* 单页出错不影响另一处 */ }
    try { initLocal(); } catch (e) { /* 同上 */ }
  }

  // 暴露给页面与测试：create 建实例；_quad 是 Barnes–Hut 四叉树（便于单测精度）
  window.GraphView = {
    create: create,
    _quad: { makeQuad: makeQuad, insert: quadInsert, force: quadForce }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
