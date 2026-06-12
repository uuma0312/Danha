// B5: 10K bounding spheres x 100 frames x 6 planes — frustum cull.
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cmath>

struct V4 { double x, y, z, w; };

static V4 mat4_rotate_y_times_v(double angle, V4 v) {
    double c = std::cos(angle);
    double s = std::sin(angle);
    // Standard Y-rotation matrix:
    //   [ c  0  s  0]
    //   [ 0  1  0  0]
    //   [-s  0  c  0]
    //   [ 0  0  0  1]
    return V4{
        c * v.x + s * v.z,
        v.y,
        -s * v.x + c * v.z,
        v.w
    };
}

static inline double dot4(V4 a, V4 b) {
    return a.x*b.x + a.y*b.y + a.z*b.z + a.w*b.w;
}

int main(void) {
    constexpr int32_t N = 10000;
    constexpr int32_t FRAMES = 100;
    V4 planes[6] = {
        { 0.0,  0.0,  1.0, 30.0},
        { 0.0,  0.0, -1.0, 30.0},
        { 1.0,  0.0,  0.0, 30.0},
        {-1.0,  0.0,  0.0, 30.0},
        { 0.0,  1.0,  0.0, 30.0},
        { 0.0, -1.0,  0.0, 30.0}
    };

    V4* centers = (V4*)malloc(sizeof(V4) * N);
    double* radii = (double*)malloc(sizeof(double) * N);
    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13; state ^= state >> 17; state ^= state << 5;
        double cx = (double)(int32_t)((state >> 1) % 100u) - 50.0;
        state ^= state << 13; state ^= state >> 17; state ^= state << 5;
        double cy = (double)(int32_t)((state >> 1) % 100u) - 50.0;
        state ^= state << 13; state ^= state >> 17; state ^= state << 5;
        double cz = (double)(int32_t)((state >> 1) % 100u) - 50.0;
        centers[i] = V4{cx, cy, cz, 1.0};
        radii[i] = 1.5;
    }

    int32_t total_visible = 0;
    for (int32_t frame = 0; frame < FRAMES; frame++) {
        double angle = (double)frame * 0.01;
        for (int32_t i = 0; i < N; i++) {
            V4 c = mat4_rotate_y_times_v(angle, centers[i]);
            double r = radii[i];
            double neg_r = 0.0 - r;
            int32_t visible = 1;
            for (int32_t p = 0; p < 6; p++) {
                double d = dot4(planes[p], c);
                if (d < neg_r) {
                    visible = 0;
                }
            }
            if (visible == 1) total_visible = total_visible + 1;
        }
    }
    printf("%d\n", total_visible);
    free(centers); free(radii);
    return 0;
}
