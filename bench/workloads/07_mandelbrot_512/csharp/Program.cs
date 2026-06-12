using System;

class Bench {
    static int MandelIter(double cx, double cy, int maxIter) {
        double zx = 0.0, zy = 0.0;
        for (int i = 0; i < maxIter; i++) {
            double zx2 = zx * zx, zy2 = zy * zy;
            if (zx2 + zy2 > 4.0) return i;
            double newZx = zx2 - zy2 + cx;
            zy = 2.0 * zx * zy + cy;
            zx = newZx;
        }
        return maxIter;
    }
    static void Main() {
        int w = 512, h = 512, maxIter = 256;
        long total = 0;
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                double cx = (double)x / (double)w * 3.5 - 2.5;
                double cy = (double)y / (double)h * 2.0 - 1.0;
                total += MandelIter(cx, cy, maxIter);
            }
        }
        Console.WriteLine(total);
    }
}
