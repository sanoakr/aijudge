/* お試しコースの「最大値を求める」に出す解答（ガイドの画像用）。
 * 全ケースを通す。 */
#include <stdio.h>

int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 1;
    int max = 0;
    for (int i = 0; i < n; i++) {
        int value;
        if (scanf("%d", &value) != 1) return 1;
        if (i == 0 || value > max) max = value;
    }
    printf("%d\n", max);
    return 0;
}
