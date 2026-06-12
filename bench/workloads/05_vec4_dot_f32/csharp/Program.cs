using System;
using System.Numerics;
using System.Runtime.Intrinsics;

class Bench {
    static double DotSum(Vector128<float> a, int n) {
        double acc = 0.0;
        for (int i = 0; i < n; i++) {
            float x = (float)i;
            var b = Vector128.Create(x);
            var m = a * b;
            acc += (double)(m[0] + m[1] + m[2] + m[3]);
        }
        return acc;
    }
    static void Main() {
        var a = Vector128.Create(1.0f, 2.0f, 3.0f, 4.0f);
        double result = DotSum(a, 100000000);
        Console.WriteLine(result.ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
