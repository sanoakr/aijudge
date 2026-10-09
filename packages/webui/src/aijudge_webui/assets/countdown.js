// 締切までの残り（#73）。**サーバが渡した秒数から数える** ── 学習者の PC の
// 時計と締切を比べると、数分のずれがそのまま「あと 3 分」と表示されながら
// サーバは遅延と判定する、という食い違いになる。
//
// **1 分を切ったら秒単位。** それまでは分単位で、更新も 15 秒に 1 回で足りる
// ── 締切前は同じ画面を 90 名が開いている。
//
// 学習者の一覧と教員の問題セット一覧の両方が読む（2026-10-10）。**書き写さない**
// ── 教員が「学生にはいまどう見えているか」を確かめる表示なので、文言も区切りも
// 学生の画面と同じでなければ意味がない。
(function () {
  var fields = document.querySelectorAll("[data-deadline-in]");
  if (!fields.length) return;
  var loaded = Date.now();

  function label(left, state) {
    // 締切を過ぎたら**カウントアップ**に切り替える。「出せなくなった」と
    // 読ませないよう、減点提出できることは文言のほうで言う。
    var over = left < 0;
    var s = Math.abs(left);
    var text;
    if (s < 60) {
      text = s + " 秒";
    } else if (s < 3600) {
      text = Math.floor(s / 60) + " 分";
    } else if (s < 86400) {
      text = Math.floor(s / 3600) + " 時間 " + Math.floor((s % 3600) / 60) + " 分";
    } else {
      text = Math.floor(s / 86400) + " 日 " + Math.floor((s % 86400) / 3600) + " 時間";
    }
    if (state === "closed") return "受付終了";
    return over ? "締切から " + text + " 経過" : "残り " + text;
  }

  function tick() {
    var elapsed = Math.floor((Date.now() - loaded) / 1000);
    var soon = false;
    fields.forEach(function (el) {
      var left = parseInt(el.getAttribute("data-deadline-in"), 10) - elapsed;
      el.textContent = label(left, el.getAttribute("data-state"));
      if (Math.abs(left) < 60) soon = true;
    });
    // 分表示のあいだは 15 秒に 1 回で足りる。
    window.setTimeout(tick, soon ? 1000 : 15000);
  }
  tick();
})();
