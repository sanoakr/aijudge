/* 同じ課題の、**負の数で落ちる**解答（ガイドの画像用）。
 * 最大値を 0 から始めているので、すべて負の入力で 0 を出す。
 * 習熟度が課題ごとに割れて見えるよう、わざと一部だけ通す。 */
#include <stdio.h>

int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 1;
    int max = 0;
    for (int i = 0; i < n; i++) {
        int value;
        if (scanf("%d", &value) != 1) return 1;
        if (value > max) max = value;
    }
    printf("%d\n", max);
    return 0;
}
