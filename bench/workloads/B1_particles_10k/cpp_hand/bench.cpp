// B1: 10K particles x 600 ticks with gravity + damping.
#include <cstdio>
#include <cstdint>
#include <cstdlib>

struct Particle {
    double px, py, vx, vy;
};

int main(void) {
    constexpr int32_t N = 10000;
    constexpr int32_t TICKS = 600;
    Particle* ps = (Particle*)malloc(sizeof(Particle) * N);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        double sx = (double)(int32_t)(state & 1023u);
        double sy = (double)(int32_t)((state >> 10) & 1023u);
        ps[i] = { sx, sy, 0.5, -2.0 };
    }

    const double dt = 0.01667;
    const double gravity = 9.8;
    const double damping = 0.999;
    for (int32_t tick = 0; tick < TICKS; tick++) {
        for (int32_t j = 0; j < N; j++) {
            ps[j].vy = ps[j].vy + gravity * dt;
            ps[j].vx = ps[j].vx * damping;
            ps[j].vy = ps[j].vy * damping;
            ps[j].px = ps[j].px + ps[j].vx * dt;
            ps[j].py = ps[j].py + ps[j].vy * dt;
        }
    }

    int32_t hx = (int32_t)(ps[N - 1].px * 1000.0);
    int32_t hy = (int32_t)(ps[N - 1].py * 1000.0);
    printf("%d\n", hx * 31 + hy);
    free(ps);
    return 0;
}
