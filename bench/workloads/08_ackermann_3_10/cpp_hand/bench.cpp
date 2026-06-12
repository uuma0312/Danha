#include <cstdio>
#include <cstdint>

static int32_t ack(int32_t m, int32_t n) {
    if (m == 0) return n + 1;
    if (n == 0) return ack(m - 1, 1);
    return ack(m - 1, ack(m, n - 1));
}

int main(void) {
    printf("%d\n", ack(3, 10));
    return 0;
}
