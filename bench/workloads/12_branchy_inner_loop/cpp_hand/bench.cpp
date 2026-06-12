#include <cstdio>
#include <cstdint>

static int32_t branchy(int32_t n) {
    int32_t acc = 0;
    for (int32_t i = 0; i < n; i++) {
        int32_t x = i % 4;
        if (x == 0) acc += i;
        else if (x == 1) acc += 2 * i;
        else if (x == 2) acc -= i;
        else acc += 3;
    }
    return acc;
}

int main(void) {
    printf("%d\n", branchy(10000000));
    return 0;
}
