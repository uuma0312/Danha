#include <cstdio>
#include <cstdint>
#include <cstdlib>

int main(void) {
    int32_t n = 1000000;
    int32_t passes = 200;
    double* arr = (double*)malloc(sizeof(double) * n);
    for (int32_t i = 0; i < n; i++) arr[i] = (double)i;
    double total = 0.0;
    for (int32_t p = 0; p < passes; p++) {
        for (int32_t j = 0; j < n; j++) {
            total += arr[j];
        }
    }
    printf("%g\n", total);
    free(arr);
    return 0;
}
