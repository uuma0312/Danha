// 100K entities × 300 ticks SoA layout
#include <cstdio>
#include <cstdint>
#include <cstdlib>

int main(void) {
    int32_t n = 100000;
    double* xs = (double*)malloc(sizeof(double) * n);
    double* ys = (double*)malloc(sizeof(double) * n);
    double* vxs = (double*)malloc(sizeof(double) * n);
    double* vys = (double*)malloc(sizeof(double) * n);
    for (int32_t i = 0; i < n; i++) {
        xs[i] = (double)i; ys[i] = 0.0; vxs[i] = 1.0; vys[i] = 0.5;
    }
    double dt = 0.016;
    for (int32_t tick = 0; tick < 300; tick++) {
        for (int32_t j = 0; j < n; j++) {
            xs[j] += vxs[j] * dt;
            ys[j] += vys[j] * dt;
        }
    }
    printf("%g\n", xs[n - 1]);
    free(xs); free(ys); free(vxs); free(vys);
    return 0;
}
