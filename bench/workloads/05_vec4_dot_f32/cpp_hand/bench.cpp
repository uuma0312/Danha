// vec4f dot product — hand-rolled __m128 SSE
#include <cstdio>
#include <cstdint>
#include <immintrin.h>

static double dot_sum(__m128 a, int32_t n) {
    double acc = 0.0;
    for (int32_t i = 0; i < n; i++) {
        float x = (float)i;
        __m128 b = _mm_set1_ps(x);
        __m128 m = _mm_mul_ps(a, b);
        float s[4]; _mm_storeu_ps(s, m);
        acc += (double)(s[0] + s[1] + s[2] + s[3]);
    }
    return acc;
}

int main(void) {
    __m128 a = _mm_set_ps(4.0f, 3.0f, 2.0f, 1.0f);
    printf("%g\n", dot_sum(a, 100000000));
    return 0;
}
