#include <stdio.h>

int main() {
    int n;
    do {
        scanf("%d", &n);
    } while (n < 1);

    int max, min, sum = 0;
    for (int i=0; i<n; i++) {
        int a;
        scanf("%d", &a);
        if (i == 0 || a > max) max = a;
        if (i == 0 || a < min) min = a;
        sum += a;
    }
    printf("%d %d %.3f\n", max, min, (double)sum/n);    

    return 0;
}
