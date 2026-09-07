#include <stdio.h>
int main(void) {
    int a;
    do { scanf("%d", &a); } while (a < 1);
    int b = 0, c = 0, d = 0;
    for (int i = 0; i < a; i++) {
        int e;
        scanf("%d", &e);
        if (i == 0 || e > b) b = e;
        if (i == 0 || e < c) c = e;
        d += e;
    }
    printf("%d %d %.3f\n", b, c, (double)d / a);
    return 0;
}
