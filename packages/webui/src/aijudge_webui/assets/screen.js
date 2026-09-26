/*
 * 試験中の画面の静止画（ADR 0027・#444）。
 *
 * IDE（ide.js）と実測ページ（tools/screen_probe）の両方から使う。ここは
 * 「共有を取り、決めた時刻に 1 枚撮って渡す」だけで、送り先も記録の形も
 * 知らない（呼び出し側が onFrame / onState で受け取る）。
 *
 * 撮り方（ADR 0027 §2）:
 *   - 定期: 5〜15 秒の一様ランダム（固定間隔だと「撮った直後に切り替える」が成り立つ）
 *   - 手元の保持: 3 秒ごとに撮って直近 30 秒ぶんを持つ（送らない）。大きな貼り付けの
 *     とき、呼び出し側が flushRing() で直前の数枚を取り出す
 *   - 出来事の後: captureSoon(kind, 遅れ) で数秒後に 1 枚
 *
 * **時計は Worker で回す。** 隠れたタブではブラウザが本体のタイマーを間引く
 * （Chrome は 1 分に 1 回まで落とす）── 受験者が別のタブに切り替えたまさに
 * そのときに撮れなくなる。Worker からのメッセージは間引かれない。
 *
 * **フレームは映像トラックから直接取る**（ImageCapture があれば）。<video> を
 * 経由すると、隠れたタブで再生が止められたときに古い 1 枚を撮り続ける。
 */
(function () {
  "use strict";

  var DEFAULTS = {
    randomMinMs: 5000,
    randomMaxMs: 15000,
    ringIntervalMs: 3000,
    ringKeepMs: 30000,
    tickMs: 500,
    maxWidth: 1280,
    quality: 0.6,
    // 定期撮影で「直前とほぼ同じ」とみなす差（縮小した濃淡の平均の差、0〜255）。
    sameThreshold: 2.0,
  };

  // 時計の Worker。本体のタイマーは隠れたタブで間引かれるので、ここで刻む。
  function startClock(ms, onTick) {
    var source = "var t=setInterval(function(){postMessage(0)}," + ms + ");" +
      "onmessage=function(){clearInterval(t);close()}";
    try {
      var url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
      var worker = new Worker(url);
      worker.onmessage = onTick;
      return { stop: function () { worker.postMessage(0); URL.revokeObjectURL(url); }, kind: "worker" };
    } catch (e) {
      // Worker を作れない環境（CSP 等）。間引かれうることを呼び出し側に伝える。
      var id = setInterval(onTick, ms);
      return { stop: function () { clearInterval(id); }, kind: "timer" };
    }
  }

  function randomDelay(o) {
    return o.randomMinMs + Math.random() * (o.randomMaxMs - o.randomMinMs);
  }

  // 縮小した濃淡の指紋。定期撮影の重複を見分けるだけに使う。
  function fingerprint(source, w, h) {
    var c = document.createElement("canvas");
    c.width = 32; c.height = 18;
    var ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(source, 0, 0, w, h, 0, 0, 32, 18);
    var data = ctx.getImageData(0, 0, 32, 18).data;
    var out = new Uint8Array(32 * 18);
    for (var i = 0, j = 0; i < data.length; i += 4, j++) {
      out[j] = (data[i] * 3 + data[i + 1] * 6 + data[i + 2]) / 10;
    }
    return out;
  }

  function difference(a, b) {
    if (!a || !b) return Infinity;
    var sum = 0;
    for (var i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
    return sum / a.length;
  }

  function start(options) {
    var o = {};
    for (var k in DEFAULTS) o[k] = DEFAULTS[k];
    for (var key in options || {}) o[key] = options[key];
    var onFrame = o.onFrame || function () {};
    var onState = o.onState || function () {};

    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      onState("unsupported", { reason: "getDisplayMedia is not available" });
      return Promise.reject(new Error("unsupported"));
    }

    return navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "monitor", frameRate: 5 },
      audio: false,
      // Chrome/Edge: 画面全体を選ばせる（タブ・ウィンドウを選択肢の先頭に出さない）。
      monitorTypeSurfaces: "include",
      selfBrowserSurface: "exclude",
      surfaceSwitching: "exclude",
    }).then(function (stream) {
      var track = stream.getVideoTracks()[0];
      var surface = (track.getSettings && track.getSettings().displaySurface) || null;
      if (surface && surface !== "monitor") {
        // タブやウィンドウだけの共有は受け付けない（ADR 0027 §3）。
        stream.getTracks().forEach(function (t) { t.stop(); });
        onState("wrong_surface", { surface: surface });
        throw new Error("wrong_surface");
      }

      var capture = typeof ImageCapture === "function" ? new ImageCapture(track) : null;
      var video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      video.play().catch(function () {});

      var state = {
        stopped: false,
        ring: [],            // { t, blob, width, height }
        lastFingerprint: null,
        nextRandom: performance.now() + randomDelay(o),
        nextRing: performance.now() + o.ringIntervalMs,
        pending: [],         // { at, kind }
        busy: false,
      };

      function frameSource() {
        if (capture) {
          return capture.grabFrame().then(function (bitmap) {
            return { source: bitmap, w: bitmap.width, h: bitmap.height, method: "imagecapture" };
          });
        }
        if (!video.videoWidth) return Promise.reject(new Error("no frame yet"));
        return Promise.resolve({ source: video, w: video.videoWidth, h: video.videoHeight, method: "video" });
      }

      function grab(dedupe) {
        return frameSource().then(function (f) {
          var fp = fingerprint(f.source, f.w, f.h);
          if (dedupe && difference(fp, state.lastFingerprint) < o.sameThreshold) {
            if (f.source.close) f.source.close();
            return null;
          }
          var scale = Math.min(1, o.maxWidth / f.w);
          var canvas = document.createElement("canvas");
          canvas.width = Math.round(f.w * scale);
          canvas.height = Math.round(f.h * scale);
          canvas.getContext("2d").drawImage(f.source, 0, 0, canvas.width, canvas.height);
          if (f.source.close) f.source.close();
          return new Promise(function (resolve) {
            canvas.toBlob(function (blob) {
              resolve(blob && { blob: blob, width: canvas.width, height: canvas.height,
                                method: f.method, fingerprint: fp });
            }, "image/jpeg", o.quality);
          });
        });
      }

      function emit(shot, kind, takenAt) {
        state.lastFingerprint = shot.fingerprint;
        onFrame(shot.blob, {
          kind: kind, t: takenAt, width: shot.width, height: shot.height,
          method: shot.method, hidden: document.hidden,
        });
      }

      function tick() {
        if (state.stopped || state.busy) return;
        var now = performance.now();
        var job = null;
        var due = state.pending.filter(function (p) { return p.at <= now; });
        if (due.length) {
          job = { kind: due[0].kind, send: true, dedupe: false };
          state.pending = state.pending.filter(function (p) { return p !== due[0]; });
        } else if (now >= state.nextRandom) {
          job = { kind: "random", send: true, dedupe: true };
          state.nextRandom = now + randomDelay(o);
        } else if (now >= state.nextRing) {
          job = { kind: "ring", send: false, dedupe: false };
          state.nextRing = now + o.ringIntervalMs;
        }
        if (!job) return;
        state.busy = true;
        var takenAt = now;
        grab(job.dedupe).then(function (shot) {
          if (!shot) return;
          if (job.send) emit(shot, job.kind, takenAt);
          else {
            state.ring.push({ t: takenAt, blob: shot.blob, width: shot.width,
                              height: shot.height, method: shot.method });
            state.ring = state.ring.filter(function (r) { return r.t >= takenAt - o.ringKeepMs; });
          }
        }).catch(function () {}).then(function () { state.busy = false; });
      }

      var clock = startClock(o.tickMs, tick);

      function end(reason) {
        if (state.stopped) return;
        state.stopped = true;
        clock.stop();
        stream.getTracks().forEach(function (t) { t.stop(); });
        onState("stopped", { reason: reason });
      }
      track.addEventListener("ended", function () { end("ended"); });

      onState("sharing", { surface: surface || "unknown", clock: clock.kind,
                           method: capture ? "imagecapture" : "video" });

      return {
        // 直前 withinMs ミリ秒ぶんの保持を渡す（大きな貼り付けの前の画面）。
        flushRing: function (kind, withinMs) {
          var since = performance.now() - withinMs;
          state.ring.filter(function (r) { return r.t >= since; }).forEach(function (r) {
            onFrame(r.blob, { kind: kind, t: r.t, width: r.width, height: r.height,
                              method: r.method, hidden: document.hidden, fromRing: true });
          });
        },
        // delayMs 後に 1 枚（離脱の 1.5 秒後、貼り付けの 1 秒後など）。
        captureSoon: function (kind, delayMs) {
          state.pending.push({ at: performance.now() + (delayMs || 0), kind: kind });
        },
        stop: function () { end("stopped_by_page"); },
        isSharing: function () { return !state.stopped; },
      };
    }, function (error) {
      if (error && error.message === "wrong_surface") throw error;
      onState("denied", { reason: error && error.name });
      throw error;
    });
  }

  window.AijudgeScreen = { start: start, DEFAULTS: DEFAULTS };
})();
