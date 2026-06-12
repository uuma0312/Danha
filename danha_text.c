// danha_text.c — 단아 텍스트 렌더링 (Stage 85)
// stb_truetype 기반. TTF 로드 → ASCII 32-127을 RGBA atlas에 한 번 bake →
// gl_text_draw가 글자별 atlas UV로 sprite_batch에 quad add.
//
// 사용:
//   font = gl_font_load("C:/Windows/Fonts/arial.ttf", 24.0)
//   sprite_batch_begin()
//   gl_text_draw(font, "Hello", 10.0, 10.0, 1.0, 1.0, 1.0, 1.0)
//   sprite_batch_end()
//
// 외부 의존: stb_truetype.h (같은 디렉토리), danha_gl의 _dnh_textures + gl_sprite_add.

#define STB_TRUETYPE_IMPLEMENTATION
#include "stb_truetype.h"

#include <SDL2/SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <GL/gl.h>
#else
#  include <GL/gl.h>
#endif

// danha_gl.c의 텍스처 슬롯 시스템에 직접 접근하기 위한 외부 선언.
// danha_gl.o와 같이 링크되니 같은 _dnh_textures 배열을 본다.
#define _DNH_TEX_MAX 2048
typedef struct { unsigned int tex; int w; int h; int valid; } _DnhTexSlot_ext;
extern _DnhTexSlot_ext _dnh_textures[_DNH_TEX_MAX];
extern int _dnh_next_tex;

// batch path의 add 함수 — danha_gl.c에 있음.
extern void gl_sprite_add(int tex, double x, double y, double w, double h,
                          double u0, double v0, double u1, double v1,
                          double r, double g, double b, double a);
extern void gl_sprite_begin(void);
extern void gl_sprite_end(void);

#define _DNH_FONT_MAX  16
#define _DNH_FIRST_CH  32
#define _DNH_NUM_CH    96

typedef struct {
    int   tex_id;
    int   atlas_w, atlas_h;
    float pixel_height;
    stbtt_bakedchar cdata[_DNH_NUM_CH];
    int   valid;
} _DnhFont;

static _DnhFont _fonts[_DNH_FONT_MAX];
static int _next_font = 1;

// sprite batch 활성 플래그를 외부에서 못 보기에 자체 트래킹은 안 함.
// gl_text_draw는 호출자가 sprite_batch_begin/end 안에서 부르는 걸 권장.
// 편의를 위해 gl_text_quick — 자체 begin/end로 한 줄 그리기 (덜 효율적).

int gl_font_load(const char* path, double pixel_size) {
    if (_next_font >= _DNH_FONT_MAX) {
        fprintf(stderr, "[text] 폰트 슬롯 초과 (max %d)\n", _DNH_FONT_MAX);
        return 0;
    }
    if (_dnh_next_tex >= _DNH_TEX_MAX) {
        fprintf(stderr, "[text] 텍스처 슬롯 초과\n");
        return 0;
    }
    FILE* f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "[text] fopen 실패: %s\n", path);
        return 0;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0) { fclose(f); return 0; }
    unsigned char* ttf_buf = (unsigned char*)malloc((size_t)sz);
    if (!ttf_buf) { fclose(f); return 0; }
    if (fread(ttf_buf, 1, (size_t)sz, f) != (size_t)sz) {
        fclose(f);
        free(ttf_buf);
        return 0;
    }
    fclose(f);

    int atlas_w = 512, atlas_h = 512;
    unsigned char* alpha = (unsigned char*)calloc((size_t)(atlas_w * atlas_h), 1);
    if (!alpha) { free(ttf_buf); return 0; }
    int fid_tmp = _next_font;
    int r = stbtt_BakeFontBitmap(ttf_buf, 0, (float)pixel_size, alpha, atlas_w, atlas_h,
                                  _DNH_FIRST_CH, _DNH_NUM_CH, _fonts[fid_tmp].cdata);
    free(ttf_buf);
    if (r <= 0) {
        fprintf(stderr, "[text] BakeFontBitmap 실패 — atlas 작거나 pixel_size 큼\n");
        free(alpha);
        return 0;
    }

    // 8-bit alpha → RGBA 변환 (글자색은 batch tint로, alpha는 폰트에서)
    unsigned char* rgba = (unsigned char*)malloc((size_t)(atlas_w * atlas_h * 4));
    if (!rgba) { free(alpha); return 0; }
    for (int i = 0; i < atlas_w * atlas_h; i++) {
        rgba[i*4+0] = 255;
        rgba[i*4+1] = 255;
        rgba[i*4+2] = 255;
        rgba[i*4+3] = alpha[i];
    }
    free(alpha);

    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, atlas_w, atlas_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, 0x812F, 0x812F);  // GL_TEXTURE_WRAP_S/T = CLAMP_TO_EDGE
    glTexParameteri(GL_TEXTURE_2D, 0x8072, 0x812F);  // (alt enum, OK either way)
    free(rgba);

    int tex_id = _dnh_next_tex++;
    _dnh_textures[tex_id].tex   = tex;
    _dnh_textures[tex_id].w     = atlas_w;
    _dnh_textures[tex_id].h     = atlas_h;
    _dnh_textures[tex_id].valid = 1;

    int fid = _next_font++;
    _fonts[fid].tex_id       = tex_id;
    _fonts[fid].atlas_w      = atlas_w;
    _fonts[fid].atlas_h      = atlas_h;
    _fonts[fid].pixel_height = (float)pixel_size;
    _fonts[fid].valid        = 1;
    return fid;
}

// 한 줄 텍스트를 batch에 add. baseline = y + pixel_height (좌상단 좌표계).
// 호출자가 sprite_batch_begin/end 안에서 부를 것 — 자동 wrap 안 함 (perf).
void gl_text_draw(int font, const char* text, double x, double y,
                  double r, double g, double b, double a) {
    if (font <= 0 || font >= _next_font || !_fonts[font].valid || !text) return;
    _DnhFont* F = &_fonts[font];
    float fx = (float)x;
    float fy = (float)y + F->pixel_height;
    const unsigned char* s = (const unsigned char*)text;
    while (*s) {
        unsigned char c = *s++;
        if (c < _DNH_FIRST_CH || c >= _DNH_FIRST_CH + _DNH_NUM_CH) continue;
        stbtt_aligned_quad q;
        stbtt_GetBakedQuad(F->cdata, F->atlas_w, F->atlas_h,
                           c - _DNH_FIRST_CH, &fx, &fy, &q, 1);
        gl_sprite_add(F->tex_id,
            (double)q.x0, (double)q.y0,
            (double)(q.x1 - q.x0), (double)(q.y1 - q.y0),
            (double)q.s0, (double)q.t0, (double)q.s1, (double)q.t1,
            r, g, b, a);
    }
}

// 자체 begin/end로 한 줄. batch 안 활성일 때 편의용.
void gl_text_quick(int font, const char* text, double x, double y,
                   double r, double g, double b, double a) {
    gl_sprite_begin();
    gl_text_draw(font, text, x, y, r, g, b, a);
    gl_sprite_end();
}

double gl_text_measure_w(int font, const char* text) {
    if (font <= 0 || font >= _next_font || !_fonts[font].valid || !text) return 0.0;
    _DnhFont* F = &_fonts[font];
    float fx = 0.0f, fy = 0.0f;
    const unsigned char* s = (const unsigned char*)text;
    while (*s) {
        unsigned char c = *s++;
        if (c < _DNH_FIRST_CH || c >= _DNH_FIRST_CH + _DNH_NUM_CH) continue;
        stbtt_aligned_quad q;
        stbtt_GetBakedQuad(F->cdata, F->atlas_w, F->atlas_h,
                           c - _DNH_FIRST_CH, &fx, &fy, &q, 1);
    }
    return (double)fx;
}

double gl_text_height(int font) {
    if (font <= 0 || font >= _next_font || !_fonts[font].valid) return 0.0;
    return (double)_fonts[font].pixel_height;
}
