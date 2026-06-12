using System;

class Bench {
    static void Main() {
        int n = 1000000;
        int passes = 200;
        double[] arr = new double[n];
        for (int i = 0; i < n; i++) arr[i] = (double)i;
        double total = 0.0;
        for (int p = 0; p < passes; p++) {
            for (int j = 0; j < n; j++) {
                total += arr[j];
            }
        }
        Console.WriteLine(total.ToString("G", System.Globalization.CultureInfo.InvariantCulture).ToLowerInvariant());
    }
}
