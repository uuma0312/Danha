// 4M random scatter — xorshift32 PRNG, scatter writes into 4M-int array.
#include <cstdio>
#include <cstdint>
#include <vector>

int main(void) {
    constexpr int32_t N = 4194304;
    constexpr int32_t ITERS = 4194304;
    std::vector<int32_t> a(N, 0);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < ITERS; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        uint32_t idx = state & (uint32_t)(N - 1);
        a[idx] += 1;
    }

    int32_t sum = 0;
    for (int32_t i = 0; i < N; i += 1024) {
        sum += a[i];
    }
    printf("%d\n", sum);
    return 0;
}
