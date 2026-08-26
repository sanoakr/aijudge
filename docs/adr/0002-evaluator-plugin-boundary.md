# ADR 0002: 科目の知識は Evaluator プラグインに閉じ込める

- ステータス: 採用
- 日付: 2026-08-27
- 関連: 設計原則 P1, P3

## 背景

aiJudge はプログラミング・数学・物理化学・レポートを同一基盤で採点する。
これらは評価方法がまったく違う。

| 科目 | 決定的に判定できるもの | AI に任せるもの |
|------|----------------------|----------------|
| プログラミング | テストケース、静的解析 | 設計の妥当性、可読性、アプローチ |
| 数学 | CAS による数式同値、数値 | 途中式の論理、証明の筋 |
| 物理化学 | 単位、有効数字、許容誤差 | 考察、近似の妥当性 |
| レポート | 必須節、引用形式、字数 | 論理構成、主張と根拠の対応 |

素直に実装すると採点エンジンが科目ごとの分岐だらけになり、
新科目の追加がエンジン本体の改修になる。これでは複数科目運用に耐えない。

## 決定

**採点エンジンは科目を知らない。** 科目固有の知識は 2 か所にだけ置く。

1. **Evaluator プラグイン**（`evaluators/*`）— 実際の判定ロジック。
   `packages/core` にのみ依存し、他のプラグインを知らない。
2. **科目プロファイル**（`subjects/*.yaml`）— どの Evaluator を
   どの順で有効にするかの宣言。コードではなく設定。

```yaml
# subjects/math_calculus.yaml
input:         {allow_handwriting: true, transcription: math_vlm}
normalizers:   [latex_normalize]
deterministic: [cas_equivalence, numeric_checker]
ai_evaluators: [rubric_criterion_judge, step_by_step_judge]
aggregation:   weighted_sum
review_policy: {confidence_below: 0.75, always_review_if_weight_over: 0.4}
```

エンジンの責務は「プロファイルを読み、Evaluator を順に呼び、結果を集約する」だけ。

### 実行順序を型で守る

Evaluator は `EvaluatorKind` で決定的（deterministic）と AI に分かれ、
決定的が必ず先に走る。決定的評価が確定させた観点（`CriterionScore.conclusive`）は
AI の判定で上書きされない。この規則は規約ではなく実装で守る。

- `CriterionScore.conclusive = True` は決定的評価器しか付けられない
  （`grading.py` のバリデータが AI からの conclusive を拒否する）。
- 同一観点に複数の評価器が答えたときの優先順位は `resolve_conflicts()` が決める。

LLM は数値計算を間違える。その弱点をプロンプトの工夫ではなく、
アーキテクチャ上「AI に数値判定をさせない」ことで回避する。

## 根拠

- **新科目の追加コストがこの設計の価値そのもの。** PoC-3 の合格基準を
  「`packages/core` と `packages/grading` への変更が 50 行未満」と定量化してある。
  この数字を超えたら P1 が破綻しており、設計を見直す。
- **AI 生成問題も同じ経路を通る。** `TaskVersion.provenance` に生成情報は残るが、
  採点側はそれを見ない。作問の実装を変えても採点に波及しない。

## 帰結

- Evaluator は独立してテストできる。ゴールデンデータ（`evals/`）も
  Evaluator 単位で持つ。
- 科目プロファイルの妥当性検査が必要になる（存在しない Evaluator ID を書けてしまう）。
  ロード時に検証し、起動時に落とす。
- 「どの Evaluator がどの観点を担当するか」は `RubricCriterion.evaluator_id` で
  問題側からも指定できる。プロファイルが既定、観点指定が上書き。

## 却下した案

**科目ごとに採点エンジンを持つ。** 共通化できるはずの HITL 振り分け・再現性記録・
イベント発行が科目の数だけ重複する。科目が増えるほど負債が増える。

**すべて AI に任せて分岐をなくす。** 単位や数値の判定精度が実用水準に届かず、
説明可能性（P4）も落ちる。決定的に判定できるものを AI に任せる理由がない。
