"""全リポジトリを 1 つのトランザクションに載せる口（段階的な立て直し 2-2）。

保存層の `SqlUnitOfWork` は 13 のリポジトリを持つが、Protocol
（`aijudge_submission.UnitOfWork`）が言っていたのは 5 つだけだった。残りの
8 つは具象の SQL クラスを通してしか型が付かず、コース運営の業務を保存層から
切り離せなかった（段階 3 の `course_admin` は保存層を import しない）。

**ここに置くのは、どのサブシステムにも置けないから。** 13 の Protocol は
6 つのサブシステムに散っていて、サブシステムどうしは import しない
（`subsystems-are-independent`）。束ねる型だけを持つ package を分け、
使ってよいのは合成ルートだけにした（`unit-of-work-is-for-the-apps`）。

`aijudge_submission.UnitOfWork` は残す ── 提出の受付（`intake`）が要るのは
提出・採点・レビュー・ジョブ・送信箱の 5 つだけで、狭い口のほうがインメモリで
試せる。
"""

from __future__ import annotations

from .protocols import StoreUnitOfWork, UnitOfWork

__all__ = ["StoreUnitOfWork", "UnitOfWork"]
