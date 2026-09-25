"""C の提出が数学関数（libm）をリンクできる。

glibc では libm が libc と別なので、`-lm` が無いと `sqrt()` を使っただけの正しい
提出がリンクで落ち、コンパイルエラーとして返る。講義で普通に出す関数である
（prog2 の ex04・ex05）。macOS の cc は libm を libSystem に含めるので、手元で
コンパイルが通ることは確かめにならない ── 引数の並びそのものを固定する。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aijudge_toolchain import LANGUAGES


def test_the_c_compile_links_libm_after_the_source() -> None:
    argv = LANGUAGES["c"].compile_argv
    assert argv is not None
    assert "-lm" in argv
    # リンカは左から未解決の記号を解く。ソースより前の -lm は効かない。
    assert argv.index("-lm") > argv.index(LANGUAGES["c"].source_name)


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")
def test_a_program_using_sqrt_compiles_with_the_same_arguments(tmp_path: Path) -> None:
    language = LANGUAGES["c"]
    (tmp_path / language.source_name).write_text(
        "#include <math.h>\n#include <stdio.h>\n"
        'int main(void) { double x; if (scanf("%lf", &x) != 1) return 0;'
        ' printf("%.3f\\n", sqrt(x)); return 0; }\n',
        encoding="utf-8",
    )
    assert language.compile_argv is not None
    subprocess.run(language.compile_argv, cwd=tmp_path, check=True, capture_output=True)
    result = subprocess.run(
        language.run_argv, cwd=tmp_path, input="2\n", capture_output=True, text=True, check=True
    )
    assert result.stdout == "1.414\n"
