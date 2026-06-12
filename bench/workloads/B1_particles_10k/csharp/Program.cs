using System;

class Bench {
    struct Particle {
        public double px, py, vx, vy;
    }
    static void Main() {
        const int N = 10000;
        const int TICKS = 600;
        var ps = new Particle[N];

        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            double sx = (double)(int)(state & 1023u);
            double sy = (double)(int)((state >> 10) & 1023u);
            ps[i] = new Particle { px = sx, py = sy, vx = 0.5, vy = -2.0 };
        }

        const double dt = 0.01667;
        const double gravity = 9.8;
        const double damping = 0.999;
        for (int tick = 0; tick < TICKS; tick++) {
            for (int j = 0; j < N; j++) {
                ps[j].vy = ps[j].vy + gravity * dt;
                ps[j].vx = ps[j].vx * damping;
                ps[j].vy = ps[j].vy * damping;
                ps[j].px = ps[j].px + ps[j].vx * dt;
                ps[j].py = ps[j].py + ps[j].vy * dt;
            }
        }

        int hx = (int)(ps[N - 1].px * 1000.0);
        int hy = (int)(ps[N - 1].py * 1000.0);
        Console.WriteLine(hx * 31 + hy);
    }
}
