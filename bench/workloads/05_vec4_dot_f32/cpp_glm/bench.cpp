// vec4f dot product — glm idiomatic
#include <cstdio>
#include <cstdint>
#include <glm/glm.hpp>

int main(void) {
    glm::vec4 a(1.0f, 2.0f, 3.0f, 4.0f);
    double acc = 0.0;
    int32_t n = 100000000;
    for (int32_t i = 0; i < n; i++) {
        float x = (float)i;
        glm::vec4 b(x, x, x, x);
        acc += (double)glm::dot(a, b);
    }
    printf("%g\n", acc);
    return 0;
}
