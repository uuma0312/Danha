// vec4f normalize hot loop — SSE
#include <cstdio>
#include <cstdint>
#include <cmath>
#include <immintrin.h>

int main(void) {
    int32_t n = 10000000;
    double acc = 0.0;
    for (int32_t i = 0; i < n; i++) {
        float x = (float)i;
        __m128 v = _mm_set_ps(x + 3.0f, x + 2.0f, x + 1.0f, x);
        __m128 sq = _mm_mul_ps(v, v);
        float s[4]; _mm_storeu_ps(s, sq);
        float mag = std::sqrt(s[0] + s[1] + s[2] + s[3]);
        __m128 mv = _mm_set1_ps(mag);
        __m128 u = _mm_div_ps(v, mv);
        float u4[4]; _mm_storeu_ps(u4, u);
        acc += (double)u4[0] + (double)u4[1] + (double)u4[2] + (double)u4[3];
    }
    printf("%g\n", acc);
    return 0;
}
