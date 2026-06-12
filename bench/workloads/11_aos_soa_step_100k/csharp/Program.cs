using System;

class Bench {
    static void Main() {
        int n = 100000;
        var xs = new double[n];
        var ys = new double[n];
        var vxs = new double[n];
        var vys = new double[n];
        for (int i = 0; i < n; i++) {
            xs[i] = (double)i; ys[i] = 0.0; vxs[i] = 1.0; vys[i] = 0.5;
        }
        double dt = 0.016;
        for (int tick = 0; tick < 300; tick++) {
            for (int j = 0; j < n; j++) {
                xs[j] += vxs[j] * dt;
                ys[j] += vys[j] * dt;
            }
        }
        Console.WriteLine(xs[n - 1].ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
