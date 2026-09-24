/* ブラウザ IDE の画面（docs/design/online-coding-test.md §5）。
 *
 * サーバとは HTTP だけで話す（WebSocket は使わない・ADR 0024 §5）。
 *
 *   PUT  /ide/tasks/{id}/buffer   自動保存（変更があるときだけ、10 秒ごと）
 *   POST /ide/tasks/{id}/run      試しの実行を積む → GET /ide/runs/{run} を 0.5 秒ごと
 *   POST /ide/tasks/{id}/submit   提出（既存の提出と同じ Submission になる）
 *
 * 補完は出さない（設計書 §5.3 の「切」）。括弧を閉じる・字下げ・対応する
 * 括弧の強調・色分けだけを使う。AI による補完は作らない。
 */
(function () {
  "use strict";

  var config = JSON.parse(document.getElementById("ide-config").textContent);
  var tabCount = config.tabs.length;
  var editors = [];
  var models = [];
  var state = [];
  var monacoRef = null;

  for (var i = 0; i < tabCount; i++) {
    state.push({
      suffix: config.selected[i],
      savedSource: config.sources[i],
      submitted: config.submitted[i],
      lastSubmittedHash: config.lastSubmittedHash[i],
      currentHash: null,
      running: false,
    });
  }

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return (root || document).querySelectorAll(selector); }

  function formatOf(index, suffix) {
    var list = config.formats[index];
    for (var k = 0; k < list.length; k++) if (list[k].suffix === suffix) return list[k];
    return list[0];
  }

  function source(index) {
    return models[index] ? models[index].getValue() : config.sources[index];
  }

  function bytes(text) { return new TextEncoder().encode(text).length; }

  function sha256(text) {
    // crypto.subtle は安全な接続（https・localhost）でしか使えない。無ければ
    // 「変更あり」の判定を諦める ── 表示が 1 つ減るだけで、動作は変わらない。
    if (!window.crypto || !window.crypto.subtle) return Promise.resolve(null);
    return window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)).then(
      function (buffer) {
        return Array.prototype.map
          .call(new Uint8Array(buffer), function (b) { return b.toString(16).padStart(2, "0"); })
          .join("");
      }
    );
  }

  function send(method, url, body, keepalive) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      keepalive: !!keepalive,
    }).then(function (response) {
      return response
        .json()
        .catch(function () { return {}; })
        .then(function (data) { return { ok: response.ok, status: response.status, data: data }; });
    });
  }

  function detailOf(result, fallback) {
    var detail = result && result.data && result.data.detail;
    if (typeof detail === "string" && detail) return detail;
    return fallback;
  }

  // -- タブ ------------------------------------------------------------------

  function selectTab(index) {
    $all(".ide-tab").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.getAttribute("data-index") == index ? "true" : "false");
    });
    $all(".ide-panel").forEach(function (panel, k) { panel.hidden = k !== index; });
    if (editors[index]) editors[index].layout();
  }

  $all(".ide-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      selectTab(parseInt(tab.getAttribute("data-index"), 10));
    });
  });

  function paintTabState(index) {
    var el = $('[data-tab-state="' + index + '"]');
    if (!el) return;
    var s = state[index];
    var changed = s.lastSubmittedHash && s.currentHash && s.currentHash !== s.lastSubmittedHash;
    if (!s.submitted) {
      el.textContent = "未提出";
      el.className = "ide-tab-state pill attn";
    } else if (changed) {
      el.textContent = "提出 " + s.submitted + " 回・変更あり";
      el.className = "ide-tab-state pill";
    } else {
      el.textContent = "提出 " + s.submitted + " 回";
      el.className = "ide-tab-state pill ok";
    }
  }

  function refreshHash(index) {
    sha256(source(index)).then(function (hash) {
      state[index].currentHash = hash;
      paintTabState(index);
    });
  }

  // -- 形式と実行の可否 ------------------------------------------------------

  function paintRunnable(index) {
    var runnable = config.runnable[index] && config.runnable[index] === state[index].suffix;
    var runButton = $('.ide-run[data-tab="' + index + '"]');
    var runbox = $('[data-runbox="' + index + '"]');
    var note = $('[data-run-note="' + index + '"]');
    runButton.hidden = !runnable;
    runbox.hidden = !runnable;
    $all('.ide-run-sample[data-tab="' + index + '"]').forEach(function (b) {
      b.disabled = !runnable;
    });
    if (runnable) {
      note.textContent = "";
    } else if (config.runnable[index]) {
      note.textContent =
        "この形式では実行できません（" + config.runnable[index] + " のときだけ実行できます）。提出はできます。";
    } else if (state[index].suffix === ".md") {
      note.textContent = "テキストとして書いて提出します。";
    } else {
      note.textContent = "この課題では試しの実行はできません。提出はできます。";
    }
  }

  $all(".ide-format").forEach(function (select) {
    select.addEventListener("change", function () {
      var index = parseInt(select.getAttribute("data-tab"), 10);
      state[index].suffix = select.value;
      if (monacoRef && models[index]) {
        monacoRef.editor.setModelLanguage(models[index], formatOf(index, select.value).monaco);
      }
      paintRunnable(index);
      markDirty(index);
    });
  });

  // -- ファイルの読み込み ----------------------------------------------------

  $all(".ide-file").forEach(function (input) {
    input.addEventListener("change", function () {
      var index = parseInt(input.getAttribute("data-tab"), 10);
      var file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      if (file.size > config.maxBytes) {
        window.alert("ファイルが大きすぎます（" + Math.floor(config.maxBytes / 1024) + " KiB まで）。");
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        if (models[index] && models[index].getValue().trim()) {
          if (!window.confirm("いまの内容を、読み込んだファイルで置き換えますか？")) return;
        }
        if (models[index]) models[index].setValue(String(reader.result));
        // 拡張子が選べる形式なら、形式もそれに合わせる。
        var dot = file.name.lastIndexOf(".");
        var suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
        var select = $('.ide-format[data-tab="' + index + '"]');
        if (select && Array.prototype.some.call(select.options, function (o) { return o.value === suffix; })) {
          select.value = suffix;
          select.dispatchEvent(new Event("change"));
        }
      };
      reader.readAsText(file);
    });
  });

  // -- 自動保存 --------------------------------------------------------------

  var dirty = [];
  for (var d = 0; d < tabCount; d++) dirty.push(false);

  function markDirty(index) {
    dirty[index] = true;
    var el = $('[data-saved="' + index + '"]');
    if (el) el.textContent = "未保存の変更があります";
  }

  function save(index, keepalive) {
    if (!dirty[index]) return Promise.resolve();
    var text = source(index);
    if (bytes(text) > config.maxBytes) {
      $('[data-saved="' + index + '"]').textContent =
        "大きすぎて保存できません（" + Math.floor(config.maxBytes / 1024) + " KiB まで）";
      return Promise.resolve();
    }
    dirty[index] = false;
    return send(
      "PUT",
      "/ide/tasks/" + config.tabs[index] + "/buffer",
      { suffix: state[index].suffix, source: text },
      keepalive
    )
      .then(function (result) {
        var el = $('[data-saved="' + index + '"]');
        if (result.ok) {
          state[index].savedSource = text;
          el.textContent = "保存しました " + new Date().toLocaleTimeString("ja-JP");
        } else {
          // 保存できなくても、書いた内容はこの画面に残る。**消さない。**
          dirty[index] = true;
          el.textContent = "保存できませんでした: " + detailOf(result, "サーバが受け付けませんでした");
        }
      })
      .catch(function () {
        dirty[index] = true;
        $('[data-saved="' + index + '"]').textContent = "保存できませんでした（通信の不調）。書いた内容はこの画面に残っています";
      });
  }

  window.setInterval(function () {
    for (var k = 0; k < tabCount; k++) save(k, false);
  }, config.autosaveMs);

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") for (var k = 0; k < tabCount; k++) save(k, true);
  });

  window.addEventListener("beforeunload", function (event) {
    if (dirty.some(function (x) { return x; })) {
      for (var k = 0; k < tabCount; k++) save(k, true);
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // -- 実行 ------------------------------------------------------------------

  function showOutput(index, text, kind) {
    var el = $('[data-output="' + index + '"]');
    el.textContent = text;
    el.setAttribute("data-kind", kind || "");
  }

  function describe(outcome) {
    var lines = [];
    if (outcome.stage === "compile") {
      lines.push("── コンパイルエラー ──");
      lines.push(outcome.stderr || outcome.stdout || "(出力なし)");
      return lines.join("\n");
    }
    if (outcome.stdout) lines.push(outcome.stdout.replace(/\n$/, ""));
    if (outcome.stderr) {
      lines.push("── 標準エラー出力 ──");
      lines.push(outcome.stderr.replace(/\n$/, ""));
    }
    var status;
    if (outcome.timed_out) status = "時間切れで止めました";
    else if (outcome.signal_name) status = outcome.signal_name + " で強制終了しました";
    else status = "終了コード " + outcome.exit_code;
    lines.push("── " + status + "（" + outcome.duration_ms + " ms）" + (outcome.truncated ? "・出力は途中まで" : "") + " ──");
    return lines.join("\n");
  }

  function poll(index, runId) {
    send("GET", "/ide/runs/" + runId)
      .then(function (result) {
        if (!result.ok) {
          finishRun(index);
          showOutput(index, detailOf(result, "実行の結果を取れませんでした"), "error");
          return;
        }
        var run = result.data;
        if (run.state === "queued" || run.state === "running") {
          var waiting = run.state === "running" ? "実行中…" : run.ahead ? "順番待ち（前に " + run.ahead + " 件）…" : "まもなく実行します…";
          showOutput(index, waiting, "pending");
          window.setTimeout(function () { poll(index, runId); }, config.pollMs);
          return;
        }
        finishRun(index);
        if (run.state === "done") showOutput(index, describe(run.outcome), "done");
        else if (run.state === "expired") showOutput(index, "混み合っていたため、実行しませんでした。もう一度実行してください。", "error");
        else showOutput(index, run.error || "実行できませんでした", "error");
      })
      .catch(function () {
        // 通信が切れただけなら、少し待って問い合わせ直す。
        window.setTimeout(function () { poll(index, runId); }, config.pollMs * 4);
      });
  }

  function finishRun(index) {
    state[index].running = false;
    $('.ide-run[data-tab="' + index + '"]').disabled = false;
  }

  function run(index, sampleName) {
    if (state[index].running) return;
    var text = source(index);
    if (!text.trim()) { showOutput(index, "コードが空です。", "error"); return; }
    var body = { suffix: state[index].suffix, source: text };
    if (sampleName) body.sample_name = sampleName;
    else body.stdin = $('[data-stdin="' + index + '"]').value;
    state[index].running = true;
    $('.ide-run[data-tab="' + index + '"]').disabled = true;
    showOutput(index, "送信中…", "pending");
    send("POST", "/ide/tasks/" + config.tabs[index] + "/run", body)
      .then(function (result) {
        if (result.status === 202) { poll(index, result.data.id); return; }
        finishRun(index);
        showOutput(index, detailOf(result, "実行を受け付けませんでした"), "error");
      })
      .catch(function () {
        finishRun(index);
        showOutput(index, "送信できませんでした（通信の不調）。", "error");
      });
  }

  $all(".ide-run").forEach(function (button) {
    button.addEventListener("click", function () {
      run(parseInt(button.getAttribute("data-tab"), 10), null);
    });
  });
  $all(".ide-run-sample").forEach(function (button) {
    button.addEventListener("click", function () {
      run(parseInt(button.getAttribute("data-tab"), 10), button.getAttribute("data-sample"));
    });
  });

  // -- 提出 ------------------------------------------------------------------

  $all(".ide-submit").forEach(function (button) {
    button.addEventListener("click", function () {
      var index = parseInt(button.getAttribute("data-tab"), 10);
      var text = source(index);
      var note = $('[data-submitted="' + index + '"]');
      if (!text.trim()) { note.textContent = "内容が空です。"; return; }
      var label = formatOf(index, state[index].suffix).label;
      if (!window.confirm("いまの内容を「" + label + "」として提出しますか？")) return;
      button.disabled = true;
      note.textContent = "提出しています…";
      send("POST", "/ide/tasks/" + config.tabs[index] + "/submit", { suffix: state[index].suffix, source: text })
        .then(function (result) {
          button.disabled = false;
          if (!result.ok) {
            note.textContent = "提出できませんでした: " + detailOf(result, "サーバが受け付けませんでした");
            return;
          }
          var data = result.data;
          state[index].submitted = Math.max(state[index].submitted, data.attempt);
          state[index].lastSubmittedHash = data.content_hash;
          state[index].currentHash = data.content_hash;
          state[index].savedSource = text;
          dirty[index] = false;
          paintTabState(index);
          note.textContent = "";
          var message = data.deduplicated ? "同じ内容を提出済みです（" + data.attempt + " 回目）。" : "提出しました（" + data.attempt + " 回目）。";
          note.appendChild(document.createTextNode(message + " "));
          var link = document.createElement("a");
          link.href = data.url;
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent = "結果を見る";
          note.appendChild(link);
        })
        .catch(function () {
          button.disabled = false;
          note.textContent = "提出できませんでした（通信の不調）。もう一度押してください。";
        });
    });
  });

  // -- 残り時間 --------------------------------------------------------------

  (function () {
    var el = $("[data-ide-remaining]");
    if (!el || config.remaining === null) return;
    var loaded = Date.now();
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function tick() {
      var left = config.remaining - Math.floor((Date.now() - loaded) / 1000);
      if (left <= 0) {
        el.textContent = "受付終了";
        el.setAttribute("data-over", "1");
        return;
      }
      var h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
      el.textContent = "残り " + pad(h) + ":" + pad(m) + ":" + pad(s);
      el.toggleAttribute("data-soon", left < 300);
      window.setTimeout(tick, 1000);
    }
    tick();
  })();

  // -- エディタ --------------------------------------------------------------

  function theme() {
    var forced = document.documentElement.getAttribute("data-theme");
    if (forced === "dark") return "vs-dark";
    if (forced === "light") return "vs";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "vs-dark" : "vs";
  }

  for (var p = 0; p < tabCount; p++) { paintRunnable(p); paintTabState(p); }

  // Monaco は同じ配信元に置いてある（外部の CDN から読まない・P7）。
  var base = window.location.origin + config.monacoBase;
  window.MonacoEnvironment = {
    // ワーカーは同じ配信元から読む。作れない環境（CSP など）では Monaco が
    // 画面のスレッドで動かす ── 色分けと入力の補助はワーカーに依らない。
    getWorkerUrl: function () {
      var code =
        "self.MonacoEnvironment={baseUrl:'" + base + "'};importScripts('" + base + "vs/base/worker/workerMain.js');";
      return URL.createObjectURL(new Blob([code], { type: "text/javascript" }));
    },
  };
  // 日本語の文言（`vs/nls.messages.ja.js`）は AMD のモジュールで、ローダーの
  // 設定から読ませる。`<script>` で先に読むとローダーより前に `define` を呼んで落ちる。
  window.require.config({
    paths: { vs: base + "vs" },
    "vs/nls": { availableLanguages: { "*": "ja" } },
  });
  window.require(["vs/editor/editor.main"], function () {
    monacoRef = window.monaco;
    for (var k = 0; k < tabCount; k++) {
      (function (index) {
        var model = monacoRef.editor.createModel(
          config.sources[index],
          formatOf(index, state[index].suffix).monaco
        );
        models[index] = model;
        editors[index] = monacoRef.editor.create($('[data-editor="' + index + '"]'), {
          model: model,
          theme: theme(),
          automaticLayout: true,
          minimap: { enabled: false },
          fontSize: 14,
          tabSize: 4,
          insertSpaces: true,
          // 入力の補助だけを使う（設計書 §5.3 の「切」）。
          autoClosingBrackets: "always",
          autoClosingQuotes: "always",
          autoIndent: "full",
          matchBrackets: "always",
          quickSuggestions: false,
          suggestOnTriggerCharacters: false,
          wordBasedSuggestions: "off",
          parameterHints: { enabled: false },
          snippetSuggestions: "none",
          inlineSuggest: { enabled: false },
          acceptSuggestionOnEnter: "off",
          tabCompletion: "off",
          scrollBeyondLastLine: false,
        });
        model.onDidChangeContent(function () {
          markDirty(index);
          window.clearTimeout(state[index].hashTimer);
          state[index].hashTimer = window.setTimeout(function () { refreshHash(index); }, 500);
        });
        refreshHash(index);
      })(k);
    }
  });
})();
