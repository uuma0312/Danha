// mat4 * vec4 — glm idiomatic
#include <cstdio>
#include <cstdint>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

int main(void) {
    glm::dvec4 v(1.0, 2.0, 3.0, 4.0);
    double acc = 0.0;
    int32_t n = 10000000;
    for (int32_t i = 0; i < n; i++) {
        glm::dmat4 m = glm::rotate(glm::dmat4(1.0), (double)i, glm::dvec3(0.0, 1.0, 0.0));
        glm::dvec4 r = m * v;
        acc += r.x + r.y + r.z + r.w;
    }
    printf("%g\n", acc);
    return 0;
}
