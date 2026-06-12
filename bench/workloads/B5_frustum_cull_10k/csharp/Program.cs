using System;

class Bench {
    struct V4 { public double x, y, z, w; }

    static V4 Mat4RotateYTimesV(double angle, V4 v) {
        double c = Math.Cos(angle);
        double s = Math.Sin(angle);
        return new V4 {
            x = c * v.x + s * v.z,
            y = v.y,
            z = -s * v.x + c * v.z,
            w = v.w
        };
    }

    static double Dot4(V4 a, V4 b) {
        return a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w;
    }

    static void Main() {
        const int N = 10000;
        const int FRAMES = 100;
        var planes = new V4[] {
            new V4 { x =  0.0, y =  0.0, z =  1.0, w = 30.0},
            new V4 { x =  0.0, y =  0.0, z = -1.0, w = 30.0},
            new V4 { x =  1.0, y =  0.0, z =  0.0, w = 30.0},
            new V4 { x = -1.0, y =  0.0, z =  0.0, w = 30.0},
            new V4 { x =  0.0, y =  1.0, z =  0.0, w = 30.0},
            new V4 { x =  0.0, y = -1.0, z =  0.0, w = 30.0}
        };
        var centers = new V4[N];
        var radii = new double[N];
        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            double cx = (double)(int)((state >> 1) % 100u) - 50.0;
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            double cy = (double)(int)((state >> 1) % 100u) - 50.0;
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            double cz = (double)(int)((state >> 1) % 100u) - 50.0;
            centers[i] = new V4 { x = cx, y = cy, z = cz, w = 1.0 };
            radii[i] = 1.5;
        }

        int total_visible = 0;
        for (int frame = 0; frame < FRAMES; frame++) {
            double angle = (double)frame * 0.01;
            for (int i = 0; i < N; i++) {
                V4 c = Mat4RotateYTimesV(angle, centers[i]);
                double r = radii[i];
                double neg_r = 0.0 - r;
                int visible = 1;
                for (int p = 0; p < 6; p++) {
                    double d = Dot4(planes[p], c);
                    if (d < neg_r) {
                        visible = 0;
                    }
                }
                if (visible == 1) total_visible = total_visible + 1;
            }
        }
        Console.WriteLine(total_visible);
    }
}
