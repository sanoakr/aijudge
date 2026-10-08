/*
 * 教員画面の自動更新（2026-10-08）。
 *
 * 残数のバッジ・一覧の行・確定処理の件数は、サーバが描いた時点の値で固まっていて、
 * 開き直さないと変わらなかった。ここでは同じ URL を一定間隔で取り直し、
 * `data-live="<名前>"` を付けた区画だけを差し替える。
 *
 *   data-live="名前"            区画。中身が変わっていたら差し替える
 *   data-live-rows              区画の子を「行」として突き合わせる（表の tbody など）
 *   data-live-key="鍵"          行の鍵。同じ鍵の行は残し、無くなった行は消し、増えた行は足す
 *
 * **入力中のものは壊さない。** 区画（行）の中の欄にフォーカスがある、または既定値から
 * 変わっているなら、その区画（行）は触らない。確定の根拠を書いている最中に、一覧が
 * 差し替わって文字が消えるのが、いちばん困る。
 *
 * - 見えていないタブでは取りに行かない。見えたとき、古ければすぐ取る
 * - 失敗したら間隔を倍にして（上限あり）やり直す。ログインが切れたら止めて知らせる
 * - 取得には `X-Aijudge-Live: 1` を付ける。サーバは一度だけ出す知らせを消さない
 *   （`aijudge_reviewconsole.live_poll`）
 * - 差し替えたら、区画に `aijudge:live` を送る（行を押して開く動きなどを付け直す）
 */
(function () {
  "use strict";

  // 読み込んだ <script> の `data-interval`（ミリ秒）で間隔を変えられる（試験用。既定は 30 秒）。
  var self = document.currentScript;
  var BASE_INTERVAL_MS = (self && Number(self.getAttribute("data-interval"))) || 30000;
  var MAX_INTERVAL_MS = 240000;
  var JITTER_MS = 3000;
  var STALE_AFTER_MS = 15000;
  var MAX_BACKOFF_STEPS = 3;

  function regions() {
    return document.querySelectorAll("[data-live]");
  }
  if (!regions().length || !window.fetch || !window.DOMParser) return;

  var timer = null;
  var running = false;
  var stopped = false;
  var failures = 0;
  var lastOk = Date.now();
  var statusEl = null;

  // -- 入力中か ------------------------------------------------------------------

  function isBusy(node) {
    var active = document.activeElement;
    if (
      active &&
      active !== document.body &&
      node.contains(active) &&
      /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)
    ) {
      return true;
    }
    var fields = node.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (field.type === "hidden") continue;
      if (field.tagName === "SELECT") {
        for (var j = 0; j < field.options.length; j++) {
          if (field.options[j].selected !== field.options[j].defaultSelected) return true;
        }
        continue;
      }
      if (field.type === "checkbox" || field.type === "radio") {
        if (field.checked !== field.defaultChecked) return true;
        continue;
      }
      if (field.value !== field.defaultValue) return true;
    }
    return false;
  }

  // -- 変わったか -----------------------------------------------------------------
  //
  // 描画済みの DOM は JS が属性を足している（行を押して開く動きの `data-clickable`・
  // `style`）ので、HTML の文字列では比べない。文字と、リンクと、見た目の印（class）。
  function signature(el) {
    var parts = [
      el.textContent.replace(/\s+/g, " ").trim(),
      el.getAttribute("class") || "",
      el.getAttribute("data-href") || "",
    ];
    var inner = el.querySelectorAll("[class], [href], [data-href]");
    for (var i = 0; i < inner.length; i++) {
      parts.push(
        inner[i].getAttribute("class") || "",
        inner[i].getAttribute("href") || "",
        inner[i].getAttribute("data-href") || ""
      );
    }
    return parts.join("|");
  }

  // <details> の開き閉じは、教員が決めたものを残す（同じ id、無ければ同じ順番）。
  function carryOpen(from, to) {
    var olds = from.querySelectorAll("details");
    var news = to.querySelectorAll("details");
    for (var i = 0; i < olds.length && i < news.length; i++) {
      if ((olds[i].id || "") !== (news[i].id || "")) continue;
      if (olds[i].open) news[i].setAttribute("open", "");
      else news[i].removeAttribute("open");
    }
  }

  function keyOf(el, index) {
    return el.getAttribute("data-live-key") || "~" + index + el.tagName;
  }

  // -- 差し替え -------------------------------------------------------------------

  function syncLeaf(region, fresh) {
    if (signature(region) === signature(fresh)) return false;
    if (isBusy(region)) return false;
    var copy = document.importNode(fresh, true);
    carryOpen(region, copy);
    region.innerHTML = copy.innerHTML;
    if (region.className !== fresh.className) region.className = fresh.className;
    return true;
  }

  function syncRows(region, fresh) {
    var changed = false;
    var olds = {};
    Array.prototype.forEach.call(region.children, function (el, i) {
      olds[keyOf(el, i)] = el;
    });
    var seen = {};
    var previous = null;
    Array.prototype.forEach.call(fresh.children, function (source, i) {
      var key = keyOf(source, i);
      seen[key] = true;
      var node = olds[key];
      if (node) {
        if (signature(node) !== signature(source) && !isBusy(node)) {
          var replacement = document.importNode(source, true);
          carryOpen(node, replacement);
          node.replaceWith(replacement);
          node = replacement;
          changed = true;
        }
      } else {
        node = document.importNode(source, true);
        changed = true;
      }
      var wanted = previous ? previous.nextElementSibling : region.firstElementChild;
      if (node !== wanted) {
        region.insertBefore(node, wanted);
        changed = true;
      }
      previous = node;
    });
    Object.keys(olds).forEach(function (key) {
      if (seen[key]) return;
      if (isBusy(olds[key])) return;
      olds[key].remove();
      changed = true;
    });
    return changed;
  }

  function escapeName(name) {
    return window.CSS && CSS.escape ? CSS.escape(name) : name.replace(/"/g, '\\"');
  }

  function apply(doc) {
    regions().forEach(function (region) {
      var name = region.getAttribute("data-live");
      var fresh = doc.querySelector('[data-live="' + escapeName(name) + '"]');
      if (!fresh) return;
      var changed = region.hasAttribute("data-live-rows")
        ? syncRows(region, fresh)
        : syncLeaf(region, fresh);
      if (changed) region.dispatchEvent(new CustomEvent("aijudge:live", { bubbles: true }));
    });
  }

  // -- 取得 -----------------------------------------------------------------------

  function show(text) {
    if (!statusEl) {
      statusEl = document.createElement("p");
      statusEl.id = "live-status";
      statusEl.className = "desc live-status";
      statusEl.setAttribute("role", "status");
      var main = document.querySelector("main") || document.body;
      main.appendChild(statusEl);
    }
    statusEl.textContent = text;
  }

  function clock() {
    var now = new Date();
    function two(n) {
      return (n < 10 ? "0" : "") + n;
    }
    return two(now.getHours()) + ":" + two(now.getMinutes()) + ":" + two(now.getSeconds());
  }

  function loggedOut(response) {
    if (response.status === 401 || response.status === 403) return true;
    if (!response.redirected) return false;
    try {
      return /\/(login|auth)(\/|$)/.test(new URL(response.url).pathname);
    } catch (e) {
      return false;
    }
  }

  function delay() {
    var steps = Math.min(failures, MAX_BACKOFF_STEPS);
    return Math.min(BASE_INTERVAL_MS * Math.pow(2, steps), MAX_INTERVAL_MS) + Math.random() * JITTER_MS;
  }

  function schedule() {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, delay());
  }

  function tick() {
    timer = null;
    if (stopped || running) return;
    // 見えていない・確認の窓が開いている間は取りに行かない（次の機会に）。
    if (document.hidden || document.querySelector("dialog[open]")) {
      schedule();
      return;
    }
    running = true;
    fetch(location.href, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-Aijudge-Live": "1" },
    })
      .then(function (response) {
        if (loggedOut(response)) {
          stopped = true;
          show("ログインの有効期限が切れました。ページを開き直してください（自動更新を止めました）");
          return null;
        }
        if (!response.ok) throw new Error("status " + response.status);
        return response.text();
      })
      .then(function (text) {
        if (text === null) return;
        apply(new DOMParser().parseFromString(text, "text/html"));
        failures = 0;
        lastOk = Date.now();
        show("自動更新 " + clock());
      })
      .catch(function () {
        failures += 1;
        show("自動更新できませんでした（" + clock() + "）。間隔をあけて再試行します");
      })
      .then(function () {
        running = false;
        schedule();
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden || stopped || running) return;
    if (Date.now() - lastOk > STALE_AFTER_MS) {
      clearTimeout(timer);
      tick();
    }
  });

  schedule();
})();
