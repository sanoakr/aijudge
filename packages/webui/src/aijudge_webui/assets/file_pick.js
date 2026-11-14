/*
 * 複数ファイルの選択を扱う小さな道具（2026-11-15）。
 *
 * 課題の画面のファイル提出と、エディタの画面のファイル提出の両方が使う。以前の課題の画面は
 * 「1 枚選んで『この 1 枚を追加』を押す」を繰り返す作りで、アンケート（network）で
 * 「1 枚ずつ選ぶのが面倒」という声があった。ここは**選んだものを溜める規則だけ**を持つ:
 *
 *   merge   すでに選んだものに足す。同じファイル（名前と大きさが同じ）は足さず、上限を超えた分は
 *           足さない。足せなかった理由は message() が言葉にする。
 *   render  選んだものの一覧（見本・名前・大きさ・「外す」）を描く。1 つずつ外せる。
 *   bindDrop 領域へのドラッグ＆ドロップを受ける。
 *
 * 画面側が持つのは選んだファイルの配列だけで、ここは配列を書き換えない（merge は新しい配列を
 * 返す）。サーバへは何も送らない（見本の表示も手元だけ）。
 */
(function (root) {
  "use strict";

  function same(a, b) { return a.name === b.name && a.size === b.size; }

  function merge(current, incoming, limit) {
    var files = current.slice();
    var duplicates = [];
    var overflow = 0;
    Array.prototype.forEach.call(incoming || [], function (file) {
      if (files.some(function (kept) { return same(kept, file); })) {
        duplicates.push(file.name);
        return;
      }
      if (limit && files.length >= limit) { overflow += 1; return; }
      files.push(file);
    });
    return { files: files, duplicates: duplicates, overflow: overflow };
  }

  // 足せなかったものの案内。何も無ければ空の文字列。
  function message(result, limit) {
    var parts = [];
    if (result.duplicates.length) {
      parts.push("同じファイルは追加済みです: " + result.duplicates.join("、"));
    }
    if (result.overflow) {
      parts.push("1 回の提出は " + limit + " ファイルまでです（" + result.overflow + " 個は追加していません）");
    }
    return parts.join("。");
  }

  function size(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.ceil(bytes / 1024) + " KB";
  }

  function render(list, files, onRemove) {
    list.textContent = "";
    files.forEach(function (file, index) {
      var item = document.createElement("li");
      if (file.type && file.type.indexOf("image/") === 0 && root.URL && root.URL.createObjectURL) {
        // 選び間違いに気づけるように見本を出す。サーバには送らない。
        var img = document.createElement("img");
        img.src = root.URL.createObjectURL(file);
        img.alt = "";
        img.className = "ide-attach-thumb";
        img.onload = function () { root.URL.revokeObjectURL(img.src); };
        item.appendChild(img);
      }
      var name = document.createElement("span");
      name.className = "mono file-name";
      name.textContent = file.name + "（" + size(file.size) + "）";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "minor file-remove";
      remove.textContent = "外す";
      remove.setAttribute("aria-label", file.name + " を外す");
      remove.addEventListener("click", function () { onRemove(index); });
      item.appendChild(name);
      item.appendChild(remove);
      list.appendChild(item);
    });
  }

  function bindDrop(zone, onFiles) {
    function stop(event) { event.preventDefault(); }
    zone.addEventListener("dragover", function (event) {
      stop(event);
      zone.setAttribute("data-over", "true");
    });
    zone.addEventListener("dragleave", function () { zone.removeAttribute("data-over"); });
    zone.addEventListener("drop", function (event) {
      stop(event);
      zone.removeAttribute("data-over");
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) onFiles(files);
    });
  }

  root.AijudgeFilePick = { merge: merge, message: message, render: render, bindDrop: bindDrop };
})(window);
