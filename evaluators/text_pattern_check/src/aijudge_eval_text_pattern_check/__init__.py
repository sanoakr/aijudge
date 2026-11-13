"""提出物の本文に、課題が宣言した文字列があるかを**決定的に**照合する評価器。

## AI を呼ばない

読めば機械的に分かることは AI に訊かない（P3）。「この行に学籍番号が含まれる
か」は正規表現と文字列一致で決まる。**訊く必要があるのは「画像に何と書いて
あるか」だけ**で、それは抽出器（`image_text`）が受付のときに済ませている。

対象が画像から起こした本文でも、PDF から抜いた本文でも、ここは同じである
── 抽出器が違うだけで、この評価器は「本文」しか見ない。

## 課題が宣言するもの（ADR 0018）

項目は `TaskVersion.test_cases` のうち自分あてのものから取る。

    TestCase(
        name="受講者欄",
        evaluator_id="text_pattern_check",
        payload={"criterion": "nickname", "expect": "learner_reference",
                 "line_matches": r"認定証|体験編"},
    )

- `criterion` — どの観点に効くか（観点の `code`）。省略すると、この評価器を
  指名したすべての観点に効く。
- `pattern` — 満たしたと見なす正規表現。本文全体から探す。
- `expect: learner_reference` — **提出者の学籍番号**が本文にあること。
  課題側に書けない値なので、ここだけは名前で指す。
- `line_matches` — `pattern` / `expect` を**この正規表現に一致する行の
  近くだけ**から探す。省略すると本文全体を見る。
- `required` — 満たさなければ**最低段階にする**。比例配分では「0 にすべき
  提出」を表せない ── 別の講座の認定証にも修了文は書いてあるので、項目の
  一部が満たされて段階 1 になってしまう。
- `expected_count` — 「何本の提出物で満たしていれば全量か」。複数の認定証を
  まとめて提出する課題（ex01-3 など）向け。省略（0）だと今までどおり
  「どれか1本で満たせば OK」の真偽値になる。宣言すると、満たした提出物の
  **本数**を `expected_count` に対する割合として重みに割り当てる ── 3 枚中
  2 枚しか認定証が無ければ、その項目は 2/3 の重みしか稼がない。
- `distinct_by` — 同じものを二重に数えないための鍵。捕捉群を 1 つ持つ
  正規表現を**本文全体**に当て、正規化した捕捉文字列が同じ提出物は 1 件と
  数える。「異なるレッスンの認定証を何枚出したか」を数える課題（ex01-3）で、
  同じ認定証を複数枚出しても増えないようにする。**鍵が取れなかった提出物は
  そのまま数える** ── 書き起こしが読めなかっただけの提出を重複扱いすると、
  抽出器の失敗が学習者の減点として出る。
- `section` — 探す範囲を、この正規表現に一致する**見出し行の次の行から、
  次の Markdown 見出し（`#` 〜 `######`）の手前まで**に絞る。範囲の中の
  HTML コメント（`<!-- ... -->`）は読まない。
- `ignore_lines` — この正規表現に一致する行を読まない。テンプレートに
  最初から入っている質問文を、回答として数えないため。
- `min_chars` — `pattern` も `expect` も無い項目で、「何か書いてある」と
  認める文字数の下限。数えるのは文字（かな・漢字・英字）と数字だけで、
  空白と記号は数えない。同じ 1 文字の繰り返しは 0 字と同じに扱う。

## 「書いてあるか」を AI に訊かない（2026-11-13）

network の survey（aiJudge のアンケート）は 4 つの質問の下に回答があるかを
`rubric_ai_judge` で採点していた。提出 1 件につき 4 観点 × 自己一貫性の
標本数だけ LLM を呼び、問うていたのは「見出しの下に、テンプレートの質問文
以外の文字があるか」── 比べれば分かることを多数決にかけていた
（exam08 の `template_diff_check` と同じ構図）。

ただし「意味のない入力（1 文字だけ・記号や同じ文字の繰り返し）は未回答」と
いう線引きには、規則で決まらない境目がある。そこを規則で切ると誤りが確定
点として残るので、**決まらない提出は決めずにレビューへ回す**（P5）。

    何も書いていない                → 満たさない（確定）
    数えた文字が min_chars 以上     → 満たす（確定）
    文字はあるが min_chars 未満、
    記号だけ・同じ文字の繰り返し、
    見出しが見つからない            → 決まらない（確定させず、レビューへ）

決まらない観点は段階を低い側に置き、`conclusive` を立てず、確信度を
`UNDECIDED_CONFIDENCE` にする。科目の `review_policy.confidence_below`
（既定 0.75）を下回るので人が見る。AI 評価器には回らない ── 観点に
割り当てた評価器はこれなので、パイプラインが AI に渡さない。

テンプレートの本文とは突き合わせない。テンプレートは `Task.editor_template`
（版を持たない運用値）にしかなく、それで採点が変わると、同じ版の同じ提出の
再採点が別の結果になる（P8）。質問文の行は `ignore_lines` で課題が宣言する。

## 段階はこちらが決める

満たした項目の重みを、その観点が持つ段階に比例で割り当てる（`level_for`）。
2 段でも 3 段でも同じ宣言で動く。

`conclusive` は立てる ── **決定的評価の判定は確定である**（P3）。ここで
確定した観点は AI 評価器に回らない。誤りの余地は照合ではなく書き起こしの側に
あり、そちらは抽出器の記録（`TranscriptionMeta`）に残っている。例外は上の
「決まらない」だけで、照合ではなく線引きの側に迷いがあるときに限る。
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    RubricCriterion,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "text_pattern_check"

#: `expect` に書ける唯一の名前。提出者の学籍番号と照合する。
EXPECT_LEARNER_REFERENCE = "learner_reference"

#: 「決まらない」観点の確信度。科目の `review_policy.confidence_below` の既定
#: （0.75）を下回る値にして、人のレビューへ回す。0 にしないのは、確信度の
#: 最小値が総合の確信度になるので、他の観点の不確かさと区別が付かなくなるため。
UNDECIDED_CONFIDENCE = 0.5

#: 見出し行。`section` の範囲はここで終わる。
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
#: HTML コメント。複数行にまたがってもよい。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class Presence(Enum):
    """提出物 1 本について、項目が満たされたか。"""

    MET = "met"
    UNMET = "unmet"
    #: 規則では決まらない。確定させずにレビューへ回す。
    UNDECIDED = "undecided"


class PatternSpec(BaseModel):
    """照合する項目 1 つ。**課題が決める。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    criterion: str = ""
    pattern: str = ""
    expect: str = ""
    #: 探す範囲を、この正規表現に一致する行に絞る。
    line_matches: str = ""
    required: bool = False
    weight: float = 1.0
    #: 満たすべき提出物の本数。0 は「真偽値（どれか1本で可）」のまま。
    expected_count: int = Field(default=0, ge=0)
    #: 同じものを二重に数えないための鍵（捕捉群を 1 つ持つ正規表現）。
    distinct_by: str = ""
    #: 探す範囲を、この正規表現に一致する見出しの下（次の見出しの手前まで）に絞る。
    section: str = ""
    #: 読まない行（テンプレートの質問文など）。
    ignore_lines: str = ""
    #: 「何か書いてある」と認める文字数の下限。0 は今までどおり「空でない行があれば満たす」。
    min_chars: int = Field(default=0, ge=0)


def normalise(text: str) -> str:
    """照合の前に空白を落とす。

    認定証の実物は `Y230020　岡本秀和` のように**全角空白**で区切る。
    半角だけを落としていると、区切りの違いだけで一致しなくなる。
    """
    return text.replace(" ", "").replace("　", "").lower()


def reference_forms(reference: str) -> tuple[str, ...]:
    """学籍番号の書かれ方。**ログイン ID はメールのこともある。**

    `User.login` は「学籍番号でもメールでもよい」と決めてある（用途を固定
    しない）。Google ログインの運用では実際に `y230009@mail.example.ac.jp`
    が入る一方、学生が認定証に書くのは `y230009` だけである。**どちらの
    書かれ方も本人として認める** ── ここを片方に決めると、認証方式を
    変えた学期に全員が 0 点になる。
    """
    flat = normalise(reference)
    local = flat.split("@", 1)[0]
    return (flat, local) if local != flat else (flat,)


def lines_to_search(body: str, spec: PatternSpec) -> list[str]:
    """この項目が見てよい行。

    `line_matches` を書くと、そこに一致する行だけを見る。**写り込みで
    当たるのを防ぐため** ── 認定証をブラウザ枠ごと撮った提出では、タブや
    URL の文字列も本文に入る（実データ 47 件のうち 3 件）。「受講者欄に
    学籍番号があるか」を本文全体で見ると、写り込んだ番号でも当たる。
    """
    lines = section_lines(body, spec.section) if spec.section else body.splitlines()
    if lines is None:
        return []
    if spec.ignore_lines:
        ignored = re.compile(spec.ignore_lines, re.IGNORECASE)
        lines = [line for line in lines if not ignored.search(line)]
    if not spec.line_matches:
        return lines
    anchor = re.compile(spec.line_matches, re.IGNORECASE)
    return [line for line in lines if anchor.search(line)]


def section_lines(body: str, heading: str) -> list[str] | None:
    """`heading` に一致する見出しの下の行。**見出しが無ければ None**（空とは別）。

    範囲は見出しの次の行から、次の Markdown 見出しの手前まで。HTML コメントは
    読まない ── テンプレートが書き方の案内をコメントで置けるようにするため。
    見出しが無いことを空と同じにしないのは、学習者が見出しを消して回答を
    書いた提出を「何も書いていない」と確定させないため（`Presence.UNDECIDED`）。
    """
    expression = re.compile(heading, re.IGNORECASE)
    lines = _HTML_COMMENT.sub("", body).splitlines()
    for index, line in enumerate(lines):
        if _HEADING.match(line) and expression.search(line):
            rest = lines[index + 1 :]
            end = next((i for i, text in enumerate(rest) if _HEADING.match(text)), len(rest))
            return rest[:end]
    return None


def counted_chars(text: str) -> int:
    """回答として数える文字数。文字（かな・漢字・英字）と数字だけを数える。

    **同じ 1 文字の繰り返しは 0 字。** 「ああああ」や「aaaa」で字数の下限を
    越えさせないため。2 種類以上の文字があれば繰り返しを含めて数える
    （「なし」「特になし」は回答である）。
    """
    chars = [c for c in text if unicodedata.category(c)[0] in ("L", "N")]
    if len(set(chars)) <= 1:
        return 0
    return len(chars)


def presence(spec: PatternSpec, body: str, learner_reference: str | None) -> Presence:
    """提出物 1 本について、この項目は満たされたか。**ここに LLM は関与しない。**

    `Presence.UNDECIDED` を返すのは `section` か `min_chars` を宣言した項目
    だけで、それ以外は今までどおり満たすか満たさないかで決まる。
    """
    if spec.section and section_lines(body, spec.section) is None:
        return Presence.UNDECIDED
    haystack = lines_to_search(body, spec)

    if spec.expect == EXPECT_LEARNER_REFERENCE:
        # 学籍番号が分からなければ**満たしたことにしない。** 分からないまま
        # 通すと、誰の認定証でも通る。
        if not learner_reference:
            return Presence.UNMET
        flat = normalise("\n".join(haystack))
        found = any(form and form in flat for form in reference_forms(learner_reference))
        return Presence.MET if found else Presence.UNMET

    if spec.pattern:
        expression = re.compile(spec.pattern, re.IGNORECASE)
        found = any(expression.search(line) for line in haystack)
        return Presence.MET if found else Presence.UNMET

    written = "".join(line.strip() for line in haystack)
    if not written:
        return Presence.UNMET
    if spec.min_chars <= 0:
        return Presence.MET
    if counted_chars(written) >= spec.min_chars:
        return Presence.MET
    # 何かは書いてあるが、回答と言える字数に届かない・記号や繰り返しだけ。
    return Presence.UNDECIDED


def satisfies(spec: PatternSpec, body: str, learner_reference: str | None) -> bool:
    """この項目は満たされたか（決まらないものは満たさない側に数える）。"""
    return presence(spec, body, learner_reference) is Presence.MET


def distinct_key(spec: PatternSpec, body: str) -> str:
    """この提出物を他と見分ける鍵。**取れなければ空**（＝見分けられない）。

    `pattern` と違って**本文全体**に当てる。認定証では見分けたい文字列
    （レッスン名）と、それが認定証だと分かる定型文が別の行にあり、しかも
    長い講座名は途中で折り返す ── 行単位で見ていると捕まえられない。
    """
    if not spec.distinct_by:
        return ""
    found = re.compile(spec.distinct_by, re.IGNORECASE | re.DOTALL).search(body)
    if found is None:
        return ""
    return normalise(found.group(1) if found.groups() else found.group(0))


def _matching_artifacts(
    spec: PatternSpec,
    bodies: tuple[tuple[ArtifactId, str], ...],
    learner_reference: str | None,
) -> tuple[ArtifactId, ...]:
    """この項目を満たした提出物（本文ごとに独立して判定する）。

    `distinct_by` があるときは、同じ鍵の提出物を 1 件として数える ──
    同じ認定証を複数枚出しても増えない。**鍵が取れなかった提出物はそのまま
    数える** ── 書き起こしが読めなかっただけの提出を重複扱いにすると、
    抽出器の失敗が学習者の減点として出る。
    """
    hits: list[ArtifactId] = []
    seen: set[str] = set()
    for artifact_id, body in bodies:
        if not satisfies(spec, body, learner_reference):
            continue
        key = distinct_key(spec, body)
        if key:
            if key in seen:
                continue
            seen.add(key)
        hits.append(artifact_id)
    return tuple(hits)


def _is_met(spec: PatternSpec, hits: tuple[ArtifactId, ...]) -> bool:
    if spec.expected_count > 0:
        return len(hits) >= spec.expected_count
    return bool(hits)


def _weight_satisfied(spec: PatternSpec, hits: tuple[ArtifactId, ...]) -> float:
    """この項目が稼ぐ重み。`expected_count` があれば本数に比例させる。"""
    if spec.expected_count > 0:
        return spec.weight * min(len(hits), spec.expected_count) / spec.expected_count
    return spec.weight if hits else 0.0


def level_for(satisfied: float, total: float, criterion: RubricCriterion) -> int:
    """満たした項目の重みを、その観点が持つ段階に割り当てる。

    段階数は課題が決める（2 段でも 4 段でもよい）ので比率で対応させる。
    """
    levels = sorted(level.level for level in criterion.levels)
    if total <= 0 or satisfied >= total:
        return levels[-1]
    if satisfied <= 0:
        return levels[0]
    index = round(satisfied / total * (len(levels) - 1))
    return levels[max(0, min(index, len(levels) - 1))]


def specs_of(request: EvaluationRequest, criterion: RubricCriterion) -> tuple[PatternSpec, ...]:
    """この観点のために照合する項目。

    **決定的評価器には `EvaluationRequest.test_cases` が渡る**ので、そちらを
    読む（AI 評価器が `task_version` から拾うのとは経路が違う）。
    """
    specs = []
    for case in request.test_cases:
        if case.evaluator_id != EVALUATOR_ID:
            continue
        spec = _spec_of(case)
        if spec.criterion and spec.criterion != criterion.code:
            continue
        specs.append(spec)
    return tuple(specs)


def _spec_of(case: object) -> PatternSpec:
    payload = dict(getattr(case, "payload", {}) or {})
    return PatternSpec(
        name=str(getattr(case, "name", "")),
        criterion=str(payload.get("criterion") or ""),
        pattern=str(payload.get("pattern") or ""),
        expect=str(payload.get("expect") or ""),
        line_matches=str(payload.get("line_matches") or ""),
        required=bool(payload.get("required") or False),
        weight=float(getattr(case, "weight", 1.0)),
        expected_count=int(payload.get("expected_count") or 0),
        distinct_by=str(payload.get("distinct_by") or ""),
        section=str(payload.get("section") or ""),
        ignore_lines=str(payload.get("ignore_lines") or ""),
        min_chars=int(payload.get("min_chars") or 0),
    )


class TextPatternCheck:
    """本文と宣言された文字列を照合する（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC
    uses_test_cases = True
    # 入出力の組ではなく項目の並び。**`items` を名乗らない** ── 項目表の
    # 編集欄（`checklist_ai_judge` 用）は `description` と `aliases` しか
    # 持たず、保存のたびに payload を作り直すので、ここの宣言が消える。
    test_case_shape = "patterns"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        bodies = self._bodies(request)
        if not bodies:
            # 本文が無い。**0 点にしない** ── 読めなかったのか白紙なのかは
            # ここでは分からない（抽出器の記録が理由を持っている）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no extracted text to match against"},
            )

        scores: list[CriterionScore] = []
        matched: dict[str, dict[str, bool]] = {}
        undecided_by_criterion: dict[str, list[str]] = {}
        counts: dict[str, dict[str, int]] = {}
        for criterion in request.task_version.criteria:
            if criterion.evaluator_id != EVALUATOR_ID:
                continue
            specs = specs_of(request, criterion)
            if not specs:
                # **当て推量の既定を持たない。** 何を照合するかは課題ごとに違う。
                continue
            hits = {
                spec.name: _matching_artifacts(spec, bodies, request.learner_reference)
                for spec in specs
            }
            met = {spec.name: _is_met(spec, hits[spec.name]) for spec in specs}
            # 満たしていない項目のうち、規則で決まらなかったもの。1 つでもあれば
            # 観点を確定させずにレビューへ回す（モジュールの説明）。
            undecided = [
                spec.name
                for spec in specs
                if not met[spec.name]
                and any(
                    presence(spec, body, request.learner_reference) is Presence.UNDECIDED
                    for _, body in bodies
                )
            ]
            if undecided:
                undecided_by_criterion[criterion.code] = undecided
            matched[criterion.code] = met
            counts[criterion.code] = {name: len(ids) for name, ids in hits.items()}
            unmet_required = [spec.name for spec in specs if spec.required and not met[spec.name]]
            if unmet_required:
                level = min(level.level for level in criterion.levels)
            else:
                level = level_for(
                    sum(_weight_satisfied(spec, hits[spec.name]) for spec in specs),
                    sum(spec.weight for spec in specs),
                    criterion,
                )
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=level,
                    score_ratio=criterion.level_for(level).score_ratio,
                    weight=criterion.weight,
                    # **確定させる。** 決定的な照合に迷いは無い（P3）。例外は
                    # 線引きの側で決まらなかったときだけで、人のレビューへ回す。
                    confidence=UNDECIDED_CONFIDENCE if undecided else 1.0,
                    conclusive=not undecided,
                    evidence=self._evidence(request, bodies, specs, hits),
                    rationale=self._rationale(specs, hits, met, unmet_required, undecided),
                )
            )

        if not scores:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names text_pattern_check"},
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={
                "matched": matched,
                "counts": counts,
                "undecided": undecided_by_criterion,
                # **何本の提出物が読めたか**。「いくつ提出されたか」に、
                # パターンの一致不一致に関係なく答えられる値（#356 相当の質問）。
                "submitted_images": len(bodies),
                "characters": sum(len(body) for _, body in bodies),
            },
        )

    # -- internals ---------------------------------------------------------

    def _bodies(self, request: EvaluationRequest) -> tuple[tuple[ArtifactId, str], ...]:
        """照合する本文。**提出物の数だけある。**

        複数の認定証をまとめて提出する課題（ex01-3 など）では、抽出器
        （`image_text`）が画像 1 枚につき 1 本の本文を作る。最初の1本だけを
        見ると、2 枚目以降に書いてある学籍番号や講座名が採点に映らない
        ── 先頭で打ち切らず、読めた本文をすべて対象にする。

        **提出時の種類では選ばない。** 抽出器が既に本文へ直しているので、
        `kind` は「学習者が何を出したか」の記録であって「いま何が渡っている
        か」ではない（`checklist_ai_judge` と同じ判断）。
        """
        bodies: list[tuple[ArtifactId, str]] = []
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if not content:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.strip():
                bodies.append((artifact.id, text))
        return tuple(bodies)

    def _evidence(
        self,
        request: EvaluationRequest,
        bodies: tuple[tuple[ArtifactId, str], ...],
        specs: tuple[PatternSpec, ...],
        hits: dict[str, tuple[ArtifactId, ...]],
    ) -> tuple[Evidence, ...]:
        """照合に使った行を根拠にする（P4）。

        満たした提出物**ごと**に根拠を積む ── 1 本にまとめると、実際には
        2 枚目の画像に書いてあった行が 1 枚目の artifact の根拠として記録
        され、見た人が違う画像を確認することになる。満たさなかった項目は、
        何を探したか分かるように先頭の本文を根拠にする。
        """
        content_hash = {a.id: a.content_hash for a in request.submission.artifacts}
        body_of = dict(bodies)
        evidence: list[Evidence] = []
        for spec in specs:
            targets = hits[spec.name] or (bodies[0][0],)
            for artifact_id in targets:
                evidence.append(
                    Evidence(
                        artifact_id=artifact_id,
                        artifact_content_hash=content_hash.get(artifact_id, "unknown"),
                        span=WholeSpan(),
                        quote="\n".join(lines_to_search(body_of[artifact_id], spec))[:500] or None,
                        note=f"「{spec.name}」",
                    )
                )
        return tuple(evidence)

    def _rationale(
        self,
        specs: tuple[PatternSpec, ...],
        hits: dict[str, tuple[ArtifactId, ...]],
        met: dict[str, bool],
        unmet_required: list[str],
        undecided: list[str] | None = None,
    ) -> str:
        parts = []
        for spec in specs:
            if undecided and spec.name in undecided:
                parts.append(
                    f"{spec.name}は規則では判断できません（見出しが見つからない、"
                    "または短すぎる・記号や同じ文字の繰り返しだけの記入）。"
                    "人の確認に回します"
                )
            elif spec.expected_count > 0:
                parts.append(
                    f"{spec.name}は{len(hits[spec.name])}/{spec.expected_count}件見つかりました"
                )
            else:
                parts.append(
                    f"{spec.name}は{'見つかりました' if met[spec.name] else '見つかりませんでした'}"
                )
        if unmet_required:
            parts.append(f"必須の項目を満たしていません: {'・'.join(unmet_required)}")
        return "。".join(parts) + "。"


def build() -> TextPatternCheck:
    """entry point から呼ばれるファクトリ。"""
    return TextPatternCheck()


__all__ = [
    "EVALUATOR_ID",
    "EXPECT_LEARNER_REFERENCE",
    "UNDECIDED_CONFIDENCE",
    "PatternSpec",
    "Presence",
    "TextPatternCheck",
    "build",
    "counted_chars",
    "level_for",
    "lines_to_search",
    "normalise",
    "presence",
    "reference_forms",
    "satisfies",
    "section_lines",
    "specs_of",
]
