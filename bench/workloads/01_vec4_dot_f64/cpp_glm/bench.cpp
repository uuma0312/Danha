// vec4 dot product — glm header-only, idiomatic C++
#include <cstdio>
#include <cstdint>
#include <glm/glm.hpp>

static double dot_sum(glm::dvec4 a, int32_t n) {
    double acc = 0.0;
    for (int32_t i = 0; i < n; i++) {
        double x = (double)i;
        glm::dvec4 b(x, x, x, x);
        acc += glm::dot(a, b);
    }
    return acc;
}

int main(void) {
    glm::dvec4 a(1.0, 2.0, 3.0, 4.0);
    printf("%g\n", dot_sum(a, 100000000));
    return 0;
}
