// 1M particles × 60 ticks AoS update.
#include <cstdio>
#include <cstdint>
#include <cstdlib>

struct Particle {
    double px, py, vx, vy;
};

int main(void) {
    int32_t n = 1000000;
    Particle* ps = (Particle*)malloc(sizeof(Particle) * n);
    for (int32_t i = 0; i < n; i++) {
        ps[i] = { (double)i, 0.0, 1.0, 0.5 };
    }
    double dt = 0.016;
    for (int32_t tick = 0; tick < 60; tick++) {
        for (int32_t j = 0; j < n; j++) {
            ps[j].px += ps[j].vx * dt;
            ps[j].py += ps[j].vy * dt;
        }
    }
    printf("%d\n", (int32_t)(ps[n - 1].px * 1000.0));
    printf("%d\n", (int32_t)(ps[n - 1].py * 1000.0));
    free(ps);
    return 0;
}
