using System;
using System.Runtime.Intrinsics;

class Bench {
    static double DotSum(Vector256<double> a, int n) {
        double acc = 0.0;
        for (int i = 0; i < n; i++) {
            double x = (double)i;
            var b = Vector256.Create(x);
            var m = a * b;
            acc += m[0] + m[1] + m[2] + m[3];
        }
        return acc;
    }

    static void Main() {
        var a = Vector256.Create(1.0, 2.0, 3.0, 4.0);
        double result = DotSum(a, 100000000);
        // Match Danha / C++ '%g' style: "5e+16" not "50000000000000000"
        Console.WriteLine(result.ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
