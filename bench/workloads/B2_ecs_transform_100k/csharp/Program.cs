using System;

class Bench {
    struct V2 { public double x, y; }
    static void Main() {
        const int N = 100000;
        const int TICKS = 300;
        var positions  = new V2[N];
        var velocities = new V2[N];
        var rotations  = new double[N];
        var omegas     = new double[N];

        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            double px = (double)(int)(state & 1023u);
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            double py = (double)(int)(state & 1023u);
            positions[i]  = new V2 { x = px, y = py };
            velocities[i] = new V2 { x = 0.5, y = -0.3 };
            rotations[i]  = 0.0;
            omegas[i]     = 0.05;
        }

        const double dt = 0.01667;
        const double damp = 0.999;
        for (int tick = 0; tick < TICKS; tick++) {
            for (int j = 0; j < N; j++) {
                positions[j].x = positions[j].x + velocities[j].x * dt;
                positions[j].y = positions[j].y + velocities[j].y * dt;
            }
            for (int j = 0; j < N; j++) {
                rotations[j] = rotations[j] + omegas[j] * dt;
            }
            for (int j = 0; j < N; j++) {
                velocities[j].x = velocities[j].x * damp;
                velocities[j].y = velocities[j].y * damp;
                omegas[j] = omegas[j] * damp;
            }
        }

        int hx = (int)(positions[N - 1].x * 1000.0);
        int hy = (int)(positions[N - 1].y * 1000.0);
        int hr = (int)(rotations[N - 1] * 1000.0);
        Console.WriteLine(hx * 31 + hy * 17 + hr);
    }
}
