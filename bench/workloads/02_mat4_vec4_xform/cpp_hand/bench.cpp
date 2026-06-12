// mat4 * vec4 with runtime-varying rotation matrix
#include <cstdio>
#include <cstdint>
#include <cmath>
#include <immintrin.h>

struct mat4 { __m256d cols[4]; };

static inline mat4 rotate_y(double angle) {
    double c = std::cos(angle), s = std::sin(angle);
    mat4 m;
    // column-major: col0=[c,0,-s,0], col1=[0,1,0,0], col2=[s,0,c,0], col3=[0,0,0,1]
    m.cols[0] = _mm256_set_pd(0.0, -s, 0.0, c);
    m.cols[1] = _mm256_set_pd(0.0, 0.0, 1.0, 0.0);
    m.cols[2] = _mm256_set_pd(0.0, c, 0.0, s);
    m.cols[3] = _mm256_set_pd(1.0, 0.0, 0.0, 0.0);
    return m;
}

static inline __m256d mat_mul_vec(mat4 m, __m256d v) {
    double s[4]; _mm256_storeu_pd(s, v);
    __m256d r = _mm256_mul_pd(m.cols[0], _mm256_set1_pd(s[0]));
    r = _mm256_add_pd(r, _mm256_mul_pd(m.cols[1], _mm256_set1_pd(s[1])));
    r = _mm256_add_pd(r, _mm256_mul_pd(m.cols[2], _mm256_set1_pd(s[2])));
    r = _mm256_add_pd(r, _mm256_mul_pd(m.cols[3], _mm256_set1_pd(s[3])));
    return r;
}

int main(void) {
    __m256d v = _mm256_set_pd(4.0, 3.0, 2.0, 1.0);
    double acc = 0.0;
    int32_t n = 10000000;
    for (int32_t i = 0; i < n; i++) {
        mat4 m = rotate_y((double)i);
        __m256d r = mat_mul_vec(m, v);
        double s[4]; _mm256_storeu_pd(s, r);
        acc += s[0] + s[1] + s[2] + s[3];
    }
    printf("%g\n", acc);
    return 0;
}
