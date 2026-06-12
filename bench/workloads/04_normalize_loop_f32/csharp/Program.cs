using System;
using System.Numerics;

class Bench {
    static void Main() {
        int n = 10000000;
        double acc = 0.0;
        for (int i = 0; i < n; i++) {
            float x = (float)i;
            var v = new Vector4(x, x + 1.0f, x + 2.0f, x + 3.0f);
            var u = Vector4.Normalize(v);
            acc += (double)u.X + (double)u.Y + (double)u.Z + (double)u.W;
        }
        Console.WriteLine(acc.ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
