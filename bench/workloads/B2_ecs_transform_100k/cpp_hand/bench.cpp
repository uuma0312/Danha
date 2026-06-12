// B2: ECS-style transform update — 100K entities x 300 ticks x 3 systems.
#include <cstdio>
#include <cstdint>
#include <cstdlib>

struct V2 { double x, y; };

int main(void) {
    constexpr int32_t N = 100000;
    constexpr int32_t TICKS = 300;
    V2*     positions  = (V2*)malloc(sizeof(V2) * N);
    V2*     velocities = (V2*)malloc(sizeof(V2) * N);
    double* rotations  = (double*)malloc(sizeof(double) * N);
    double* omegas     = (double*)malloc(sizeof(double) * N);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        double px = (double)(int32_t)(state & 1023u);
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        double py = (double)(int32_t)(state & 1023u);
        positions[i]  = { px, py };
        velocities[i] = { 0.5, -0.3 };
        rotations[i]  = 0.0;
        omegas[i]     = 0.05;
    }

    const double dt = 0.01667;
    const double damp = 0.999;
    for (int32_t tick = 0; tick < TICKS; tick++) {
        for (int32_t j = 0; j < N; j++) {
            positions[j].x = positions[j].x + velocities[j].x * dt;
            positions[j].y = positions[j].y + velocities[j].y * dt;
        }
        for (int32_t j = 0; j < N; j++) {
            rotations[j] = rotations[j] + omegas[j] * dt;
        }
        for (int32_t j = 0; j < N; j++) {
            velocities[j].x = velocities[j].x * damp;
            velocities[j].y = velocities[j].y * damp;
            omegas[j] = omegas[j] * damp;
        }
    }

    int32_t hx = (int32_t)(positions[N - 1].x * 1000.0);
    int32_t hy = (int32_t)(positions[N - 1].y * 1000.0);
    int32_t hr = (int32_t)(rotations[N - 1] * 1000.0);
    printf("%d\n", hx * 31 + hy * 17 + hr);
    free(positions); free(velocities); free(rotations); free(omegas);
    return 0;
}
