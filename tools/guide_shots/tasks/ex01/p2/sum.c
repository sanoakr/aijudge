/* 合計を求める（参照解答） */
#include <stdio.h>

int main(void) {
    int n;
    do {
        scanf("%d", &n);
    } while (n < 1);

    int sum = 0;
    for (int i = 0; i < n; i++) {
        int a;
        scanf("%d", &a);
        sum += a;
    }
    printf("%d\n", sum);
    return 0;
}
