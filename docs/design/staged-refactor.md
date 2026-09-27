# 学期中に段階的に進める構造の立て直し（apps の肥大化と設計との乖離）

- ステータス: **計画**（段階 0 から着手）
- 日付: 2026-09-26
- 関連: #437, #438, #435, #436 / ADR 0001, 0002, 0016 / 設計方針 §2.3

## 0. 何を直すのか

2026-09-26 のシステム設計レビューで、設計（ADR 0001「サブシステムは core にだけ
依存し、連携はイベント」）と実装がずれていることが分かった。数字で言うと:

| 項目 | 実態 |
|---|---|
| src の行数 | apps 28,499 行 > packages 25,783 行 |
| `reviewconsole/manage.py` | 7,111 行・ルート 93 本・`register()` の中のクロージャ 23 個 |
| `apps/admin` | 35 モジュール。うち 23 が他のアプリ（reviewconsole・grader）から**ライブラリとして** import される |
| app から app への依存 | reviewconsole → admin、grader → admin（**grader の pyproject に admin の依存宣言が無い**）。縛る import 契約は無い |
| UnitOfWork | 保存層は 13 リポジトリを出すが、Protocol が宣言するのは 5 つ。apps は具象に頼る |
| イベント | 5 種を定義、実際に流れて使われるのは `GradingCompleted` だけ。`TaskPublished`・`CredentialIssued` は発行されない |
| 認可の判定 | 「この人はこのコースの教員か」が 4 か所に別々に書かれている |
| 画面の共通部品 | 学生画面とコンソールで `counterpart_url`・`_serve_video` など 10 余りが重複（1 つは振る舞いも違う） |

**目標**: 業務ロジックを packages に移し、apps を「HTTP と CLI の薄い合成」に戻す。
manage.py を画面の単位に分ける。設計の文書は、実装の着地点に合わせて改める。

## 1. 学期中に進めるための約束

コンソールは学期中ずっと使われる。どの段階も次を守る。

1. **振る舞いを変えない移動だけ**を 1 つの PR にする。移動と改善を混ぜない
   （改善は別の PR）。URL・テンプレート・フォームの名前・監査の文言は変えない
2. **1 PR は小さく**（目安: 移動 800 行まで）。1 PR = 1 リリース。問題が出たら
   その版だけ戻せる（`deploy/README.md`「切り戻し」の固定ファイル）
3. **移す前に、そこを守るテストがあること。** 薄い所（§4）は、先に「いまの
   振る舞いを写す」テスト（characterization test）を足してから動かす
4. **互換の出口を残す。** 移した関数は旧い場所から再エクスポートし、呼び出し側を
   順に書き換えてから最後に消す。1 回で全部を張り替えない
5. **機械で確かめる**: 全ルートの一覧（メソッド・パス）が変わらないこと（段階 0 の
   スナップショット）、import 契約、全テスト、PostgreSQL の CI
6. **出す時期を選ぶ**: **毎週木曜・金曜は授業があるので触らない**（リリースも運用機の
   作業もしない）。2026 年度後期の試験は 11/5・11/12・11/13 で、どれも木金に当たる。
   試験・締切の前日と当日もリリースしない。出したら主要画面を開いて確かめる

## 2. 着地点（どこへ移すか）

| 層 | 置くもの |
|---|---|
| `packages/core` | ドメインの型と純粋な規則（今と同じ） |
| `packages/*`（既存） | 各サブシステム。Store / Repository の **Protocol はここ** |
| **`packages/course_admin`（新設）** | コース運営の業務処理（課題の保存・改訂、問題セット、名簿、確定、KC、束、コースの複製・削除、採点設定、科目プロファイル）。**いまの `apps/admin` の中身の大半** |
| **`packages/access`（新設、または `aijudge_identity` へ）** | 認可の判定（「コースの教員か」「採点できるか」「管理者か」）を 1 か所に |
| **`packages/webapp`（新設）** | 2 つの Web アプリが共有する部品（版の表示、相手の画面への URL、動画の配信、主体の解決、監査の文脈）。`webui` は資産だけという約束（依存を持たない）なので分ける |
| `apps/admin` | CLI（`aijudge-admin`）だけ。業務は `course_admin` を呼ぶ |
| `apps/reviewconsole` | HTTP の合成。`manage/` を画面単位のモジュールに分ける |
| `apps/grader` | ワーカーとリレー。`justification` は `course_admin`（または `feedback`）から使う |

import 契約（段階 0 と各段階で足す）:

- apps どうしは import しない（**admin → 他のアプリ、他のアプリ → admin を禁止**。
  移行中は既存の import だけを `ignore_imports` で明示し、段階が進むたびに減らす）
- `course_admin` は `persistence` を import しない（Protocol 越し）。そのため
  段階 2 で UnitOfWork の Protocol を広げる

## 3. 段階

各段階の中の番号が PR の単位である。段階ごとに所要の目安を書く（1 PR = 半日〜1 日）。

### 段階 0: 守りを先に置く（2〜3 PR・振る舞いの変更なし）

0-1. **ルートのスナップショット**: reviewconsole・studentweb の全ルート（メソッド・
パス・ハンドラ名）を固定するテスト。移動でルートが消えたり増えたりしたら落ちる  
0-2. **app 間の import 契約**: いまある依存（reviewconsole → admin の 23 モジュール、
grader → admin.justification）を列挙して `ignore_imports` に入れ、それ以外を禁止する。
**これ以上増やさない**ことを先に固定する。grader の pyproject に依存を書く（書かれて
いない依存の是正）  
0-3. **薄いテストの補強**（§4 の表の「0〜1 回」の経路）: 課題の復元、グループの削除、
下書きの生成、再採点、移動、束の読み取りなど。いまの応答（ステータス・保存される
もの・監査）を写す

### 段階 1: 重複の解消（3〜4 PR・低リスク）

1-1. **認可を 1 か所に**: `_require_instructor`（manage・api・app の 4 版）を
`access` に寄せる。404 の文言の違いは引数で残す（振る舞いを変えない）  
   → **済み**: 規則は `AuthService.require_instructor`（`aijudge_identity`・一員でなければ
   `PermissionDenied`、一員だが教員でなければ下位の `NotAnInstructor`）と役割の集合
   `INSTRUCTOR_ROLES` に置いた。HTTP への読み替え（404/403 と文言）は
   `aijudge_reviewconsole.access` の 1 か所で、4 つの旧い関数はその薄い包みとして残した
   （呼び出し側は書き換えていない）。役割を直に並べていた比較 7 か所も同じ集合を指す。
   `packages/access` は新設しなかった ── 判定は既に `require_membership`・`require_grader`
   を持つ `AuthService` の仲間で、HTTP を知る部分はコンソールの外で使われていない  
1-2. **画面の共通部品**（`webapp`）: 完全に同じもの（`counterpart_url`、
`_read_copyright_notice`、`_read_app_version`、`_serve_video`）から。
`current_principal` は**キャッシュの有無が違う**ので、共通化は別の PR にし、
学生画面にもキャッシュを入れてよいかを確かめてから  
   → **前半は済み**: 4 つを `packages/webapp` に移した（契約 `webapp-is-for-the-apps`
   で、import できるのはアプリだけ）。`_serve_video` は状態オブジェクトの代わりに
   store を受け取る形にした（両アプリの違いはどちらの属性から取るかだけだった）  
   → **後半も済み**: `current_principal` も `webapp` に移し、学習者アプリにも要求内の
   キャッシュを入れた。確かめたこと: 学習者アプリで主体を引くのは要求の冒頭だけで、
   状態を変えた後に同じ要求で引き直す経路は無い（`resolve` は副作用の無い読み取り）。
   保存先を開く部分はアプリに残し、関数として渡す（`webapp` は保存層を import しない）  
1-3. `CourseRow` を直接読む SQL（`operations.list_courses`・`finalization._courses`）を
保存層のメソッドに置き換える（`kc.py` が `finalization._courses` を import している
結合もここで解く）
   → **済み**: `IdentityRepository.list_all_courses` を足し、3 か所を置き換えた。
   写しは遅延の減点・KC・ルーブリックの畳み方を読み落としていた（呼び出し側が
   まだ読んでいなかったので実害は無かった）。契約 `apps-do-not-read-tables` で、
   アプリが `aijudge_persistence.schema` を直接 import することを禁じた  

### 段階 2: UnitOfWork の Protocol を実態に合わせる（2〜3 PR）

2-1. `tasks`・`identity`・`skills`・`audit` の Protocol を、それぞれの package に
（すでにあるものは揃え、無いメソッドを足す）。インメモリ実装が持てないもの
（課題の表との結合）は #464 と同じく読み取り用の Protocol に分ける  
   → **済み**: 差分は課題と KC だけだった（identity・audit は揃っていた）。
   `TaskRepository` に `delete_task`・`save_embedding`・`list_embeddings`、
   `SkillRepository` に `save_kc`・`find_kc_by_key`・`list_kcs`・`delete_kc` を足し、
   インメモリにも実装した（同じテストを両方に当てる）。提出・採点の表と結合する
   `submission_count`・`pass_rates` は `TaskUsageQueries` に分け、保存層の口は
   `TaskStore`（両方）。`SkillRepository`・`AuditLog` を `@runtime_checkable` にし、
   適合性のテストを 4 つに広げた  
2-2. `UnitOfWork` の Protocol を 13 リポジトリに広げる（保存層だけが満たすものは
別の Protocol に）。適合性のテスト（#464 の形）を全リポジトリに  
   → **済み**: `aijudge_submission` からは他のサブシステムの Protocol を import
   できない（`subsystems-are-independent`）ので、束ねる型だけを持つ
   `packages/unit_of_work` を新設した。`UnitOfWork`（13 リポジトリ・インメモリでも
   満たせる型）と `StoreUnitOfWork`（`reviews: ReviewStore`・`tasks: TaskStore`）。
   契約 `unit-of-work-is-for-the-apps` で import できるのはアプリだけ ──
   段階 3 の `course_admin` はここに例外を足す。`aijudge_submission.UnitOfWork`
   （5 つ）は提出の受付が使う狭い口として残した。適合性のテストは Protocol の
   注釈を読んで 13 を 1 つずつ確かめ、IDE の 4 つもインメモリ側に加えた。
   13 を持つインメモリの UnitOfWork はまだ無い（`course_admin` をインメモリで
   試すなら要る）  
2-3. mypy を `packages/submission` と `packages/persistence` に広げる（#435・#429 の残り）
   → **済み**: 大半（80 件中 34 件）は、どの package も `py.typed` を持たず、
   import 先が型の無いものとして扱われていたためだった。全 package に置いた。
   残りは `dict`・`list` の型引数と使われていない `type: ignore` で、実際の
   食い違いは 1 件（習熟度の推移で `model` を文字列のまま `SkillPoint` に渡していた。
   pydantic が変換するので害は無かった）。CI の mypy は core・submission・
   persistence・unit_of_work。compose の PostgreSQL も本番・CI と同じ 18 にした  

### 段階 3: `apps/admin` → `packages/course_admin`（6〜8 PR）

葉から順に移す（admin 内の依存を調べた順序）。各 PR で旧い場所に再エクスポートを残す。

3-1. 例外（`AdminError`）を `course_admin.errors` に。多くのモジュールが
`operations` に依存しているのはこれのためだけ  
   → **済み**: `packages/course_admin`（`aijudge_course_admin`）を新設し、
   `AdminError` を `errors` に移した。`admin` の 19 モジュールは新しい場所から
   取り、うち 15 は `operations` への依存が消えた。旧い名前（`operations`・
   `aijudge_admin`）は再エクスポートで残し、同じクラスであることをテストで固定した
   （コンソールが旧い名前で `except` している）。保存層を import しない契約に入れ、
   mypy の対象にも最初から加えた  
3-2. 葉のモジュール: `roster`, `answer_mode`, `drafting`, `revision`, `syllabus`,
`test_cases`, `task_verifier`, `justification`（grader はこれを新しい場所から使う）  
   → **3 本に分けた**（計 1850 行で、目安の 800 行を超えるため）。
   **3-2a 済み**: `roster`・`answer_mode`・`justification`・`drafting`・`revision`。
   旧い場所には `sys.modules` を差し替える互換を残した（再エクスポートでなく同じ
   モジュールを指すので、旧い名前での `monkeypatch` も効く）。grader は
   `justification` を新しい場所から使い、`admin` への依存と契約の例外が 1 本消えた。
   **3-2b 済み**: `syllabus`（PDF の抽出器に依存するので、評価器と抽出器にも
   `py.typed` を置いた）。**3-2c 済み**: `test_cases`・`task_verifier`。**3-2 完了**  
3-3. `operations` → `rubric`, `groups`, `profiles`, `grading_settings`, `tasks`, `courses`  
   → **3-3a 済み**: `rubric`・`profiles`・`groups`（保存層を使わない 3 つ）。
   strict の mypy が要る最小限の注釈だけ足した。`groups` は `uow` を `object` で
   受けたまま ── `UnitOfWork` の型を付けるのは 3-3b（移動と改善を混ぜない）  
   → **3-3b 済み**: `course_admin` を契約 `unit-of-work-is-for-the-apps` の例外にし
   （例外はこれだけ、と境界テストで固定）、`groups` の `uow` を `UnitOfWork` で
   型付けした。`type: ignore` 15 個が消え、strict の mypy が呼び先を確かめる  
   → **3-3c 済み**: `aijudge_unit_of_work.Store`（`unit_of_work()` だけを持つ Protocol）を
   足した。残る `grading_settings`・`tasks`・`courses`・`operations` は `Database` を
   受けて `unit_of_work()` を開くだけなので、引数の型を `Store` にすれば移せる。
   置き場所は `UnitOfWork` の隣（2026-09-27 にユーザーと決めた）  
   → **3-3d 済み**: `grading_settings`・`tasks`・`courses` を移し、`database` 引数の型を
   `Store` にした（名前は変えないので呼び出し側はそのまま）。`grading_settings` の
   試走の課題選びは「見つからなければ None」を 1 つの値で返す形にした（`(None, None)`
   では版が無いときに課題も無いことが型に出ない）。**3-3e 済み**: `operations`（`database`
   を `Store` に）。**3-3 完了**  
3-4. `finalization`（**import 契約 `grades-do-not-read-activity`・
`grades-cannot-reach-activity-flags` と `test_boundaries` の名前を同時に書き換える**）→ `kc`  
   → **3-4a 済み**: `finalization` を移し（`database` は `Store`）、契約 2 本と
   `test_boundaries` の名前を `aijudge_course_admin.finalization` に書き換えた。旧い名前の
   ままだと、契約は中身の無い互換を見張ることになる。**3-4b 済み**: `kc`・`kc_skeleton`
   （`kc` が `kc_skeleton` を使うので一緒に）。**3-4 完了**  
3-5. `authoring` → `bundle_plan`, `bundles`, `course_copy`, `course_definition`  
   → **5 本に分けた**（依存される側から、1 本 800 行以内）: 3-5a `authoring`・`bundle_plan`、
   3-5b `bundles`、3-5c `course_definition`・`course_copy`、3-5d `course_export`、
   3-5e `course_snapshot`（#498 で足したもの）。`course_admin` は `admin` を import
   できないので、依存先が先に移っていないと移せない。**3-5a〜3-5e 済み（3-5 完了）**
   （3-5c では `course_definition` が隣に置いて読む `course_template.yaml` も移した）  
3-6. 呼び出し側（reviewconsole・grader・admin の CLI）を新しい場所に張り替え、
再エクスポートと `ignore_imports` を消す。`apps/admin` は CLI だけになる

### 段階 4: manage.py の分割（8〜10 PR）

4-1. **クロージャを外に出す**（23 個）。`register()` の中の関数をモジュール直下
（引数で `templates` などを受ける）へ。ルートは動かさない。これが済むまで
ファイルを分けられない（例: 6444 行の `_course_kcs` を 2623 行が使う）  
4-2. `manage/` パッケージを作り、`register()` が各モジュールの `register(router)` を
呼ぶ形にする（空の骨組み）  
4-3 以降. 独立している領域から順に移す:

| 順 | 領域 | ルート | 理由 |
|---|---|---|---|
| 1 | 利用者・テナント設定・科目プロファイル・自分のパスワード | 19 | 他の領域とほぼ helper を共有しない |
| 2 | 受講者・習熟度 | 6 | 独自の認可とテンプレート |
| 3 | KC 管理・シラバス | 11 | `_kc_page` を共有するので一緒に |
| 4 | グループ・問題セット・画像 | 20 | `_update_unit` など問題セットの helper がまとまっている |
| 5 | コース設定（散在する 7 本を集める）・確定 | 9 | `last_finalize` を app.py と共有（§5） |
| 6 | 課題の作成・編集・下書き・束・AI 生成 | 25 | `_save_revision`・`_kept_cases`・`_task_page` を共有。**最後に、テストを厚くしてから** |

### 段階 5: 設計の文書を着地点に合わせる（1〜2 PR）

5-1. ADR を足す: 「共有の UnitOfWork と 1 つの DB を許すモジュラーモノリス。
サブシステム間の**書き込み**は同じトランザクションでよい。イベントは
『後で・別のプロセスで起きてよいこと』（習熟度の更新など）に使う」  
5-2. 使われていないイベント（`TaskPublished`・`CredentialIssued`・
`SubmissionCreated` の購読者なし）の扱いを決める（消すか、使う予定を書く）  
5-3. ADR 0001・0002 の依存の記述（評価器が依存してよい範囲、共有してよい層）を
契約の実態に合わせる。全 ADR に状態（採用・改訂・置換）を付ける

## 4. 動かす前に厚くするテスト（段階 0-3）

調査時点（2026-09-26）で、テストからの参照が 0〜1 回の経路:

- 0 回: `groups/delete`、`tasks/{id}/restore`、`drafts/generate`
- 1 回: 再採点、課題の移動、KC 候補、参照解答、入力の提案、KC の編集、失敗の
  流し直し、束の読み取り・雛形、問題セットの初期化、`tasks/new`、`course-template.yaml`
- AI を使う経路（生成・AI 改訂・提案・参照解答）は、スクリプト応答
  （`ScriptedProvider`）で固定する

## 5. 気を付けること

- **`Console.last_*`**（画面に一度だけ出す知らせ）が manage.py と app.py にまたがる。
  分割の途中は `Console` に残し、最後に「知らせ」の仕組みとして 1 か所にまとめる
  （別 PR）
- **`current_principal` のキャッシュの違い**（§3 段階 1-2）
- `aijudge_admin/__init__.py` は全モジュールを読み込む。grader が `justification`
  だけを使っても admin 全体と保存層が読み込まれる ── 段階 3-2 で解消する
- **移行中の二重の場所**: 再エクスポートがある間、どちらからでも import できる。
  新しいコードは必ず新しい場所から import する（段階 3-6 で旧い場所を消すときに
  `test_boundaries` で残りが無いことを確かめる）

## 6. 目安

| 段階 | PR | 期間の目安（学期中、週 2〜3 PR） |
|---|---|---|
| 0 | 2〜3 | 1 週 |
| 1 | 3〜4 | 1〜2 週 |
| 2 | 2〜3 | 1 週 |
| 3 | 6〜8 | 3 週 |
| 4 | 8〜10 | 3〜4 週 |
| 5 | 1〜2 | 数日 |

段階 0〜2 は他の段階の前提なので順に。段階 3 と 4 は並行できる（触るファイルが
違う）が、同時に出すリリースは 1 つにする。**試験のある週（2026 年度後期は 11/2〜11/13）は
段階を進めず、その週は PR を溜めない。**
