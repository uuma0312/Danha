using System;

class Bench {
    struct Particle {
        public double px, py, vx, vy;
    }
    static void Main() {
        int n = 1000000;
        var ps = new Particle[n];
        for (int i = 0; i < n; i++) {
            ps[i] = new Particle { px = (double)i, py = 0.0, vx = 1.0, vy = 0.5 };
        }
        double dt = 0.016;
        for (int tick = 0; tick < 60; tick++) {
            for (int j = 0; j < n; j++) {
                ps[j].px += ps[j].vx * dt;
                ps[j].py += ps[j].vy * dt;
            }
        }
        Console.WriteLine((int)(ps[n - 1].px * 1000.0));
        Console.WriteLine((int)(ps[n - 1].py * 1000.0));
    }
}
