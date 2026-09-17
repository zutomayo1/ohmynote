/* 墨痕 PWA 客户端 —— 注册 Service Worker / 安装到桌面。
 *
 * Service Worker 负责离线阅读（缓存策略见服务端生成的 /sw.js）；
 * 这里只做两件事：
 *   1) 页面加载完成后注册 /sw.js（scope=/，覆盖全站）；
 *   2) 浏览器给出安装机会（beforeinstallprompt）时，往命令面板里加一条
 *      「安装到桌面」—— iOS 没有这个事件，按使用说明里的「添加到主屏幕」走。
 *
 * 注册失败静默忽略：PWA 是增强，坏掉不该影响正常浏览。
 */
(function () {
  'use strict';

  var InkNote = window.InkNote = window.InkNote || {};
  var installEvent = null;

  function addInstallCommand() {
    var commands = InkNote.PALETTE_COMMANDS;
    if (!commands || InkNote.__installCommandAdded) { return; }
    InkNote.__installCommandAdded = true;
    commands.push({
      id: 'install-app', title: '安装到桌面', url: null, action: 'install-app',
      icon: 'download', py: 'anzhuangdaozhuomian', abbr: 'azzm'
    });
  }

  // app.js 的命令分发会调用这个函数（见 runCommandAction）
  InkNote.promptInstall = function () {
    if (!installEvent) { return false; }
    installEvent.prompt();
    installEvent = null;
    return true;
  };

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();      // 不用浏览器的迷你横幅，走命令面板
    installEvent = event;
    addInstallCommand();
  });

  window.addEventListener('appinstalled', function () {
    installEvent = null;
    if (window.InkNote && InkNote.toast) { InkNote.toast('已添加到桌面', 'ok'); }
  });

  if (!('serviceWorker' in navigator)) { return; }
  window.addEventListener('load', function () {
    // 安装时已 skipWaiting：资源指纹一变就会装新版本并清掉旧缓存
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .catch(function () { /* 忽略：离线增强失败不影响正常使用 */ });
  });
})();
