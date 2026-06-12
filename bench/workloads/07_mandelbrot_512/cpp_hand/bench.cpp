#include <cstdio>
#include <cstdint>

static int32_t mandel_iter(double cx, double cy, int32_t max_iter) {
    double zx = 0.0, zy = 0.0;
    for (int32_t i = 0; i < max_iter; i++) {
        double zx2 = zx * zx, zy2 = zy * zy;
        if (zx2 + zy2 > 4.0) return i;
        double new_zx = zx2 - zy2 + cx;
        zy = 2.0 * zx * zy + cy;
        zx = new_zx;
    }
    return max_iter;
}

int main(void) {
    int32_t w = 512, h = 512, max_iter = 256;
    int64_t total = 0;
    for (int32_t y = 0; y < h; y++) {
        for (int32_t x = 0; x < w; x++) {
            double cx = (double)x / (double)w * 3.5 - 2.5;
            double cy = (double)y / (double)h * 2.0 - 1.0;
            total += mandel_iter(cx, cy, max_iter);
        }
    }
    printf("%lld\n", (long long)total);
    return 0;
}
