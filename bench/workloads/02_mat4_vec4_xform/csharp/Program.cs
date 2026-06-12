using System;
using System.Runtime.Intrinsics;

class Bench {
    // 4 column vectors as Vector256<double>
    struct Mat4 {
        public Vector256<double> c0, c1, c2, c3;
    }

    static Mat4 RotateY(double angle) {
        double c = Math.Cos(angle), s = Math.Sin(angle);
        var m = new Mat4 {
            c0 = Vector256.Create(c, 0.0, -s, 0.0),
            c1 = Vector256.Create(0.0, 1.0, 0.0, 0.0),
            c2 = Vector256.Create(s, 0.0, c, 0.0),
            c3 = Vector256.Create(0.0, 0.0, 0.0, 1.0)
        };
        return m;
    }

    static Vector256<double> MatMulVec(Mat4 m, Vector256<double> v) {
        var r = m.c0 * Vector256.Create(v[0]);
        r += m.c1 * Vector256.Create(v[1]);
        r += m.c2 * Vector256.Create(v[2]);
        r += m.c3 * Vector256.Create(v[3]);
        return r;
    }

    static void Main() {
        var v = Vector256.Create(1.0, 2.0, 3.0, 4.0);
        double acc = 0.0;
        int n = 10000000;
        for (int i = 0; i < n; i++) {
            var m = RotateY((double)i);
            var r = MatMulVec(m, v);
            acc += r[0] + r[1] + r[2] + r[3];
        }
        Console.WriteLine(acc.ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
