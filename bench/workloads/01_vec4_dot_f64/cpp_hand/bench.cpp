// vec4 dot product — hand-rolled <4 x double> intrinsics
#include <cstdio>
#include <cstdint>
#include <immintrin.h>

static double dot_sum(__m256d a, int32_t n) {
    double acc = 0.0;
    for (int32_t i = 0; i < n; i++) {
        double x = (double)i;
        __m256d b = _mm256_set1_pd(x);
        __m256d m = _mm256_mul_pd(a, b);
        double s[4]; _mm256_storeu_pd(s, m);
        acc += s[0] + s[1] + s[2] + s[3];
    }
    return acc;
}

int main(void) {
    __m256d a = _mm256_set_pd(4.0, 3.0, 2.0, 1.0);
    printf("%g\n", dot_sum(a, 100000000));
    return 0;
}
