// danha_gl.c — 단아 OpenGL 래퍼 (55단계)
// SDL2 + OpenGL 2.1 기반. SDL2 창에서 OpenGL 컨텍스트를 생성하고
// 기본 2D/3D 드로잉 + GLSL 셰이더 컴파일을 지원한다.

#include <SDL2/SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stddef.h>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <GL/gl.h>
   static void* _get_proc(const char* name) {
       void* p = (void*)wglGetProcAddress(name);
       if (!p) {
           HMODULE m = LoadLibraryA("opengl32.dll");
           if (m) p = (void*)GetProcAddress(m, name);
       }
       return p;
   }
#elif defined(__APPLE__)
#  include <OpenGL/gl.h>
#  include <OpenGL/glu.h>
   static void* _get_proc(const char* name) { return SDL_GL_GetProcAddress(name); }
#else
#  include <GL/gl.h>
#  include <GL/glu.h>
   static void* _get_proc(const char* name) { return SDL_GL_GetProcAddress(name); }
#endif

// ---- GL 2.0 함수 포인터 (셰이더 + VBO용) ----
#define GL_VERTEX_SHADER   0x8B31
#define GL_FRAGMENT_SHADER 0x8B30
#define GL_COMPILE_STATUS  0x8B81
#define GL_LINK_STATUS     0x8B82
#define GL_INFO_LOG_LENGTH 0x8B84
#ifndef GL_CLAMP_TO_EDGE
#  define GL_CLAMP_TO_EDGE  0x812F
#endif
#ifndef GL_BGR
#  define GL_BGR  0x80E0
#endif
#ifndef GL_BGRA
#  define GL_BGRA 0x80E1
#endif
// Stage 79: VBO/배칭용 상수
#define GL_ARRAY_BUFFER         0x8892
#define GL_ELEMENT_ARRAY_BUFFER 0x8893
#define GL_STATIC_DRAW          0x88E4
#define GL_DYNAMIC_DRAW         0x88E8
#define GL_STREAM_DRAW          0x88E0
#define GL_TEXTURE0             0x84C0

typedef unsigned int  GLuint2;
typedef int           GLint2;
typedef char          GLchar;
typedef float         GLfloat2;
typedef int           GLsizei2;
typedef unsigned char GLboolean2;
typedef ptrdiff_t     GLsizeiptr2;
typedef ptrdiff_t     GLintptr2;

typedef GLuint2  (APIENTRY *PFNGLCREATESHADERPROC)(GLenum);
typedef void     (APIENTRY *PFNGLSHADERSOURCEPROC)(GLuint2, GLsizei2, const GLchar**, const GLint2*);
typedef void     (APIENTRY *PFNGLCOMPILESHADERPROC)(GLuint2);
typedef GLuint2  (APIENTRY *PFNGLCREATEPROGRAMPROC)(void);
typedef void     (APIENTRY *PFNGLATTACHSHADERPROC)(GLuint2, GLuint2);
typedef void     (APIENTRY *PFNGLLINKPROGRAMPROC)(GLuint2);
typedef void     (APIENTRY *PFNGLUSEPROGRAMPROC)(GLuint2);
typedef void     (APIENTRY *PFNGLGETSHADERIVPROC)(GLuint2, GLenum, GLint2*);
typedef void     (APIENTRY *PFNGLGETPROGRAMIVPROC)(GLuint2, GLenum, GLint2*);
typedef void     (APIENTRY *PFNGLGETSHADERINFOLOGPROC)(GLuint2, GLsizei2, GLsizei2*, GLchar*);
typedef GLint2   (APIENTRY *PFNGLGETUNIFORMLOCATIONPROC)(GLuint2, const GLchar*);
typedef void     (APIENTRY *PFNGLUNIFORM1FPROC)(GLint2, GLfloat2);
typedef void     (APIENTRY *PFNGLUNIFORM2FPROC)(GLint2, GLfloat2, GLfloat2);
typedef void     (APIENTRY *PFNGLUNIFORM3FPROC)(GLint2, GLfloat2, GLfloat2, GLfloat2);
typedef void     (APIENTRY *PFNGLUNIFORM4FPROC)(GLint2, GLfloat2, GLfloat2, GLfloat2, GLfloat2);
typedef void     (APIENTRY *PFNGLUNIFORM1IPROC)(GLint2, GLint2);
typedef void     (APIENTRY *PFNGLDELETEPROGRAM2)(GLuint2);
typedef void     (APIENTRY *PFNGLDELETESHADER2)(GLuint2);
// Stage 79: VBO/attrib/texture-unit
typedef void     (APIENTRY *PFNGLGENBUFFERSPROC)(GLsizei2, GLuint2*);
typedef void     (APIENTRY *PFNGLDELETEBUFFERSPROC)(GLsizei2, const GLuint2*);
typedef void     (APIENTRY *PFNGLBINDBUFFERPROC)(GLenum, GLuint2);
typedef void     (APIENTRY *PFNGLBUFFERDATAPROC)(GLenum, GLsizeiptr2, const void*, GLenum);
typedef void     (APIENTRY *PFNGLBUFFERSUBDATAPROC)(GLenum, GLintptr2, GLsizeiptr2, const void*);
typedef void     (APIENTRY *PFNGLVERTEXATTRIBPOINTERPROC)(GLuint2, GLint2, GLenum, GLboolean2, GLsizei2, const void*);
typedef void     (APIENTRY *PFNGLENABLEVERTEXATTRIBARRAYPROC)(GLuint2);
typedef void     (APIENTRY *PFNGLDISABLEVERTEXATTRIBARRAYPROC)(GLuint2);
typedef void     (APIENTRY *PFNGLBINDATTRIBLOCATIONPROC)(GLuint2, GLuint2, const GLchar*);
typedef void     (APIENTRY *PFNGLACTIVETEXTUREPROC)(GLenum);
// Stage 86: 자동 밉맵 생성 (GL 3.0 또는 EXT_framebuffer_object)
typedef void     (APIENTRY *PFNGLGENERATEMIPMAPPROC)(GLenum);
#define GL_LINEAR_MIPMAP_LINEAR 0x2703
// Stage 87: FBO 후처리 (bloom)
#define GL_FRAMEBUFFER            0x8D40
#define GL_COLOR_ATTACHMENT0      0x8CE0
#define GL_FRAMEBUFFER_COMPLETE   0x8CD5
typedef void   (APIENTRY *PFNGLGENFRAMEBUFFERSPROC)(GLsizei2, GLuint2*);
typedef void   (APIENTRY *PFNGLDELETEFRAMEBUFFERSPROC)(GLsizei2, const GLuint2*);
typedef void   (APIENTRY *PFNGLBINDFRAMEBUFFERPROC)(GLenum, GLuint2);
typedef void   (APIENTRY *PFNGLFRAMEBUFFERTEXTURE2DPROC)(GLenum, GLenum, GLenum, GLuint2, GLint2);
typedef GLenum (APIENTRY *PFNGLCHECKFRAMEBUFFERSTATUSPROC)(GLenum);

static PFNGLCREATESHADERPROC      _glCreateShader      = NULL;
static PFNGLSHADERSOURCEPROC      _glShaderSource      = NULL;
static PFNGLCOMPILESHADERPROC     _glCompileShader     = NULL;
static PFNGLCREATEPROGRAMPROC     _glCreateProgram     = NULL;
static PFNGLATTACHSHADERPROC      _glAttachShader      = NULL;
static PFNGLLINKPROGRAMPROC       _glLinkProgram       = NULL;
static PFNGLUSEPROGRAMPROC        _glUseProgram        = NULL;
static PFNGLGETSHADERIVPROC       _glGetShaderiv       = NULL;
static PFNGLGETPROGRAMIVPROC      _glGetProgramiv      = NULL;
static PFNGLGETSHADERINFOLOGPROC  _glGetShaderInfoLog  = NULL;
static PFNGLGETUNIFORMLOCATIONPROC _glGetUniformLocation = NULL;
static PFNGLUNIFORM1FPROC         _glUniform1f         = NULL;
static PFNGLUNIFORM2FPROC         _glUniform2f         = NULL;
static PFNGLUNIFORM3FPROC         _glUniform3f         = NULL;
static PFNGLUNIFORM4FPROC         _glUniform4f         = NULL;
static PFNGLUNIFORM1IPROC         _glUniform1i         = NULL;
static PFNGLDELETEPROGRAM2        _glDeleteProgram2    = NULL;
static PFNGLDELETESHADER2         _glDeleteShader2     = NULL;
// Stage 79
static PFNGLGENBUFFERSPROC                _glGenBuffers                = NULL;
static PFNGLDELETEBUFFERSPROC             _glDeleteBuffers             = NULL;
static PFNGLBINDBUFFERPROC                _glBindBuffer                = NULL;
static PFNGLBUFFERDATAPROC                _glBufferData                = NULL;
static PFNGLBUFFERSUBDATAPROC             _glBufferSubData             = NULL;
static PFNGLVERTEXATTRIBPOINTERPROC       _glVertexAttribPointer       = NULL;
static PFNGLENABLEVERTEXATTRIBARRAYPROC   _glEnableVertexAttribArray   = NULL;
static PFNGLDISABLEVERTEXATTRIBARRAYPROC  _glDisableVertexAttribArray  = NULL;
static PFNGLBINDATTRIBLOCATIONPROC        _glBindAttribLocation        = NULL;
static PFNGLACTIVETEXTUREPROC             _glActiveTexture             = NULL;
static PFNGLGENERATEMIPMAPPROC            _glGenerateMipmap            = NULL;
// Stage 87
static PFNGLGENFRAMEBUFFERSPROC           _glGenFramebuffers           = NULL;
static PFNGLDELETEFRAMEBUFFERSPROC        _glDeleteFramebuffers        = NULL;
static PFNGLBINDFRAMEBUFFERPROC           _glBindFramebuffer           = NULL;
static PFNGLFRAMEBUFFERTEXTURE2DPROC      _glFramebufferTexture2D      = NULL;
static PFNGLCHECKFRAMEBUFFERSTATUSPROC    _glCheckFramebufferStatus    = NULL;

static void _load_gl2(void) {
    _glCreateShader      = (PFNGLCREATESHADERPROC)      _get_proc("glCreateShader");
    _glShaderSource      = (PFNGLSHADERSOURCEPROC)      _get_proc("glShaderSource");
    _glCompileShader     = (PFNGLCOMPILESHADERPROC)     _get_proc("glCompileShader");
    _glCreateProgram     = (PFNGLCREATEPROGRAMPROC)     _get_proc("glCreateProgram");
    _glAttachShader      = (PFNGLATTACHSHADERPROC)      _get_proc("glAttachShader");
    _glLinkProgram       = (PFNGLLINKPROGRAMPROC)       _get_proc("glLinkProgram");
    _glUseProgram        = (PFNGLUSEPROGRAMPROC)        _get_proc("glUseProgram");
    _glGetShaderiv       = (PFNGLGETSHADERIVPROC)       _get_proc("glGetShaderiv");
    _glGetProgramiv      = (PFNGLGETPROGRAMIVPROC)      _get_proc("glGetProgramiv");
    _glGetShaderInfoLog  = (PFNGLGETSHADERINFOLOGPROC)  _get_proc("glGetShaderInfoLog");
    _glGetUniformLocation= (PFNGLGETUNIFORMLOCATIONPROC)_get_proc("glGetUniformLocation");
    _glUniform1f         = (PFNGLUNIFORM1FPROC)         _get_proc("glUniform1f");
    _glUniform2f         = (PFNGLUNIFORM2FPROC)         _get_proc("glUniform2f");
    _glUniform3f         = (PFNGLUNIFORM3FPROC)         _get_proc("glUniform3f");
    _glUniform4f         = (PFNGLUNIFORM4FPROC)         _get_proc("glUniform4f");
    _glUniform1i         = (PFNGLUNIFORM1IPROC)         _get_proc("glUniform1i");
    _glDeleteProgram2    = (PFNGLDELETEPROGRAM2)        _get_proc("glDeleteProgram");
    _glDeleteShader2     = (PFNGLDELETESHADER2)         _get_proc("glDeleteShader");
    // Stage 79
    _glGenBuffers                = (PFNGLGENBUFFERSPROC)               _get_proc("glGenBuffers");
    _glDeleteBuffers             = (PFNGLDELETEBUFFERSPROC)            _get_proc("glDeleteBuffers");
    _glBindBuffer                = (PFNGLBINDBUFFERPROC)               _get_proc("glBindBuffer");
    _glBufferData                = (PFNGLBUFFERDATAPROC)               _get_proc("glBufferData");
    _glBufferSubData             = (PFNGLBUFFERSUBDATAPROC)            _get_proc("glBufferSubData");
    _glVertexAttribPointer       = (PFNGLVERTEXATTRIBPOINTERPROC)      _get_proc("glVertexAttribPointer");
    _glEnableVertexAttribArray   = (PFNGLENABLEVERTEXATTRIBARRAYPROC)  _get_proc("glEnableVertexAttribArray");
    _glDisableVertexAttribArray  = (PFNGLDISABLEVERTEXATTRIBARRAYPROC) _get_proc("glDisableVertexAttribArray");
    _glBindAttribLocation        = (PFNGLBINDATTRIBLOCATIONPROC)       _get_proc("glBindAttribLocation");
    _glActiveTexture             = (PFNGLACTIVETEXTUREPROC)            _get_proc("glActiveTexture");
    // Stage 86
    _glGenerateMipmap            = (PFNGLGENERATEMIPMAPPROC)           _get_proc("glGenerateMipmap");
    if (!_glGenerateMipmap)
        _glGenerateMipmap        = (PFNGLGENERATEMIPMAPPROC)           _get_proc("glGenerateMipmapEXT");
    // Stage 87 (FBO — GL 3.0 core 또는 EXT_framebuffer_object)
    _glGenFramebuffers          = (PFNGLGENFRAMEBUFFERSPROC)          _get_proc("glGenFramebuffers");
    if (!_glGenFramebuffers)
        _glGenFramebuffers      = (PFNGLGENFRAMEBUFFERSPROC)          _get_proc("glGenFramebuffersEXT");
    _glDeleteFramebuffers       = (PFNGLDELETEFRAMEBUFFERSPROC)       _get_proc("glDeleteFramebuffers");
    if (!_glDeleteFramebuffers)
        _glDeleteFramebuffers   = (PFNGLDELETEFRAMEBUFFERSPROC)       _get_proc("glDeleteFramebuffersEXT");
    _glBindFramebuffer          = (PFNGLBINDFRAMEBUFFERPROC)          _get_proc("glBindFramebuffer");
    if (!_glBindFramebuffer)
        _glBindFramebuffer      = (PFNGLBINDFRAMEBUFFERPROC)          _get_proc("glBindFramebufferEXT");
    _glFramebufferTexture2D     = (PFNGLFRAMEBUFFERTEXTURE2DPROC)     _get_proc("glFramebufferTexture2D");
    if (!_glFramebufferTexture2D)
        _glFramebufferTexture2D = (PFNGLFRAMEBUFFERTEXTURE2DPROC)     _get_proc("glFramebufferTexture2DEXT");
    _glCheckFramebufferStatus   = (PFNGLCHECKFRAMEBUFFERSTATUSPROC)   _get_proc("glCheckFramebufferStatus");
    if (!_glCheckFramebufferStatus)
        _glCheckFramebufferStatus = (PFNGLCHECKFRAMEBUFFERSTATUSPROC) _get_proc("glCheckFramebufferStatusEXT");
}

// Stage 86: 자동 밉맵 + trilinear filter. 텍스처 1x1이거나 알파 텍스트는 스킵 권장.
static void _dnh_apply_mipmap(void) {
    if (_glGenerateMipmap) {
        _glGenerateMipmap(GL_TEXTURE_2D);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR);
    }
}

// ---- 상태 ----
static SDL_Window*   _gl_win  = NULL;
static SDL_GLContext _gl_ctx  = NULL;
static int           _gl_w    = 0;
static int           _gl_h    = 0;
static int           _gl_run  = 1;

// ---- 창 관리 ----

int gl_open(int width, int height, const char* title) {
    if (SDL_Init(SDL_INIT_VIDEO) != 0) { fprintf(stderr, "SDL_Init: %s\n", SDL_GetError()); return 0; }
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 1);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
    // Stage 84+86: MSAA 8x — 가장자리 안티에일리어싱 (4x → 8x로 강화).
    SDL_GL_SetAttribute(SDL_GL_MULTISAMPLEBUFFERS, 1);
    SDL_GL_SetAttribute(SDL_GL_MULTISAMPLESAMPLES, 8);
    _gl_win = SDL_CreateWindow(title,
        SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
        width, height,
        SDL_WINDOW_OPENGL | SDL_WINDOW_SHOWN);
    if (!_gl_win) { fprintf(stderr, "CreateWindow: %s\n", SDL_GetError()); SDL_Quit(); return 0; }
    _gl_ctx = SDL_GL_CreateContext(_gl_win);
    if (!_gl_ctx) { fprintf(stderr, "GL context: %s\n", SDL_GetError()); SDL_DestroyWindow(_gl_win); SDL_Quit(); return 0; }
    SDL_GL_SetSwapInterval(1);
    _gl_w = width; _gl_h = height; _gl_run = 1;
    _load_gl2();
    glViewport(0, 0, width, height);
    glMatrixMode(GL_PROJECTION); glLoadIdentity();
    // 2D 좌표계: 좌상단 (0,0), 우하단 (width,height)
    glOrtho(0, (double)width, (double)height, 0, -1.0, 1.0);
    glMatrixMode(GL_MODELVIEW); glLoadIdentity();
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    // Stage 84: MSAA enable. context가 multisample 지원 안 하면 무시됨.
    glEnable(0x809D);   // GL_MULTISAMPLE (no header guarantee)
    return 1;
}

int gl_poll(void) {
    SDL_Event ev;
    while (SDL_PollEvent(&ev)) {
        if (ev.type == SDL_QUIT) _gl_run = 0;
        if (ev.type == SDL_KEYDOWN && ev.key.keysym.sym == SDLK_ESCAPE) _gl_run = 0;
    }
    return _gl_run;
}

void gl_swap(void)  { if (_gl_win) SDL_GL_SwapWindow(_gl_win); }

void gl_close(void) {
    if (_glUseProgram) _glUseProgram(0);
    if (_gl_ctx) { SDL_GL_DeleteContext(_gl_ctx); _gl_ctx = NULL; }
    if (_gl_win) { SDL_DestroyWindow(_gl_win); _gl_win = NULL; }
    SDL_Quit();
}

int gl_width(void)  { return _gl_w; }
int gl_height(void) { return _gl_h; }

// ---- 드로잉 ----

void gl_clear(float r, float g, float b, float a) {
    glClearColor(r, g, b, a);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
}

void gl_triangle(float x1, float y1, float x2, float y2, float x3, float y3,
                  float r, float g, float b, float a) {
    if (_glUseProgram) _glUseProgram(0);  // 고정 파이프라인으로 복귀
    glColor4f(r, g, b, a);
    glBegin(GL_TRIANGLES);
    glVertex2f(x1, y1); glVertex2f(x2, y2); glVertex2f(x3, y3);
    glEnd();
}

void gl_rect(float x, float y, float w, float h,
             float r, float g, float b, float a) {
    if (_glUseProgram) _glUseProgram(0);
    glColor4f(r, g, b, a);
    glBegin(GL_QUADS);
    glVertex2f(x,   y);   glVertex2f(x+w, y);
    glVertex2f(x+w, y+h); glVertex2f(x,   y+h);
    glEnd();
}

void gl_line(float x1, float y1, float x2, float y2,
             float r, float g, float b, float a) {
    if (_glUseProgram) _glUseProgram(0);
    glColor4f(r, g, b, a);
    glBegin(GL_LINES);
    glVertex2f(x1, y1); glVertex2f(x2, y2);
    glEnd();
}

void gl_circle(float cx, float cy, float radius, int segs,
               float r, float g, float b, float a) {
    if (_glUseProgram) _glUseProgram(0);
    glColor4f(r, g, b, a);
    glBegin(GL_TRIANGLE_FAN);
    glVertex2f(cx, cy);
    for (int i = 0; i <= segs; i++) {
        float ang = 2.0f * 3.14159265f * (float)i / (float)segs;
        glVertex2f(cx + radius * cosf(ang), cy + radius * sinf(ang));
    }
    glEnd();
}

void gl_point(float x, float y, float size,
              float r, float g, float b, float a) {
    if (_glUseProgram) _glUseProgram(0);
    glPointSize(size);
    glColor4f(r, g, b, a);
    glBegin(GL_POINTS);
    glVertex2f(x, y);
    glEnd();
}

// ---- 텍스처 (Stage 69: Ari 스프라이트 렌더러 기반) ----
// SDL_LoadBMP로 BMP 로드 → RGBA8888로 변환 → OpenGL 2.1 텍스처에 업로드.
// 사용자에게는 slot 인덱스(1..MAX-1)를 ID로 반환. 0 = invalid.

#define _DNH_TEX_MAX 2048
typedef struct { GLuint tex; int w; int h; int valid; } _DnhTexSlot;
// Stage 85: 외부(_dnh_text.c)에서 접근 가능하도록 non-static
_DnhTexSlot _dnh_textures[_DNH_TEX_MAX];
int _dnh_next_tex = 1;

int gl_texture_load(const char* path) {
    if (!path) return 0;
    if (_dnh_next_tex >= _DNH_TEX_MAX) {
        fprintf(stderr, "[GL] 텍스처 슬롯 초과 (max %d)\n", _DNH_TEX_MAX);
        return 0;
    }
    SDL_Surface* raw = SDL_LoadBMP(path);
    if (!raw) {
        fprintf(stderr, "[GL] SDL_LoadBMP 실패 (%s): %s\n", path, SDL_GetError());
        return 0;
    }
    SDL_Surface* rgba = SDL_ConvertSurfaceFormat(raw, SDL_PIXELFORMAT_ABGR8888, 0);
    SDL_FreeSurface(raw);
    if (!rgba) {
        fprintf(stderr, "[GL] SDL_ConvertSurfaceFormat 실패: %s\n", SDL_GetError());
        return 0;
    }

    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA,
                 rgba->w, rgba->h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba->pixels);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    _dnh_apply_mipmap();   // Stage 86: 축소 시 매끈

    int id = _dnh_next_tex++;
    _dnh_textures[id].tex   = tex;
    _dnh_textures[id].w     = rgba->w;
    _dnh_textures[id].h     = rgba->h;
    _dnh_textures[id].valid = 1;
    SDL_FreeSurface(rgba);
    return id;
}

int gl_texture_w(int id) {
    if (id <= 0 || id >= _dnh_next_tex || !_dnh_textures[id].valid) return 0;
    return _dnh_textures[id].w;
}
int gl_texture_h(int id) {
    if (id <= 0 || id >= _dnh_next_tex || !_dnh_textures[id].valid) return 0;
    return _dnh_textures[id].h;
}

void gl_texture_draw(int id, double x, double y, double w, double h,
                     double r, double g, double b, double a) {
    if (id <= 0 || id >= _dnh_next_tex || !_dnh_textures[id].valid) return;
    if (_glUseProgram) _glUseProgram(0);
    glEnable(GL_TEXTURE_2D);
    glBindTexture(GL_TEXTURE_2D, _dnh_textures[id].tex);
    glColor4f((float)r, (float)g, (float)b, (float)a);
    glBegin(GL_QUADS);
        glTexCoord2f(0.0f, 0.0f); glVertex2f((float)x,     (float)y);
        glTexCoord2f(1.0f, 0.0f); glVertex2f((float)(x+w), (float)y);
        glTexCoord2f(1.0f, 1.0f); glVertex2f((float)(x+w), (float)(y+h));
        glTexCoord2f(0.0f, 1.0f); glVertex2f((float)x,     (float)(y+h));
    glEnd();
    glDisable(GL_TEXTURE_2D);
}

void gl_texture_draw_uv(int id, double x, double y, double w, double h,
                         double u0, double v0, double u1, double v1,
                         double r, double g, double b, double a) {
    if (id <= 0 || id >= _dnh_next_tex || !_dnh_textures[id].valid) return;
    if (_glUseProgram) _glUseProgram(0);
    glEnable(GL_TEXTURE_2D);
    glBindTexture(GL_TEXTURE_2D, _dnh_textures[id].tex);
    glColor4f((float)r, (float)g, (float)b, (float)a);
    glBegin(GL_QUADS);
        glTexCoord2f((float)u0, (float)v0); glVertex2f((float)x,     (float)y);
        glTexCoord2f((float)u1, (float)v0); glVertex2f((float)(x+w), (float)y);
        glTexCoord2f((float)u1, (float)v1); glVertex2f((float)(x+w), (float)(y+h));
        glTexCoord2f((float)u0, (float)v1); glVertex2f((float)x,     (float)(y+h));
    glEnd();
    glDisable(GL_TEXTURE_2D);
}

// Forward declare for atlas → batch path
void gl_sprite_add(int tex, double x, double y, double w, double h,
                   double u0, double v0, double u1, double v1,
                   double r, double g, double b, double a);

// ---- Stage 81: 텍스처 아틀라스 (자동 packing) ----
// 여러 BMP를 한 GL 텍스처에 shelf-fit packing.
// 사용 흐름:
//   atlas = gl_atlas_new(1024, 1024)
//   p1 = gl_atlas_add(atlas, "player.bmp")     // sub-sprite handle
//   e1 = gl_atlas_add(atlas, "enemy.bmp")
//   gl_atlas_finalize(atlas)                   // 이후 add 안 됨, GL 업로드
//   gl_atlas_draw_sub(p1, x, y)                // 또는 batch에서 sub 정보 query
// 이러면 player+enemy가 같은 atlas 텍스처라 batch에서 1 draw call로 묶임.

#define _DNH_ATLAS_MAX  16
#define _DNH_SUB_MAX    1024

typedef struct {
    int   atlas_id;       // 어느 atlas
    int   x, y;           // atlas 내 픽셀 위치
    int   w, h;
    double u0, v0, u1, v1;
} _DnhSubSprite;

typedef struct {
    int        w, h;
    unsigned char* pixels;   // CPU staging (finalize 전), finalize 후 free
    int        shelf_x;
    int        shelf_y;
    int        shelf_h;
    int        finalized;    // 1 = GL 업로드 완료, 더 이상 add 안 됨
    int        tex_id;       // _dnh_textures slot id (finalize 후 유효)
    int        valid;
} _DnhAtlas;

static _DnhAtlas    _atlases[_DNH_ATLAS_MAX];
static int          _next_atlas = 1;
static _DnhSubSprite _subs[_DNH_SUB_MAX];
static int          _next_sub = 1;

int gl_atlas_new(int w, int h) {
    if (_next_atlas >= _DNH_ATLAS_MAX) {
        fprintf(stderr, "[GL] 아틀라스 슬롯 초과\n");
        return 0;
    }
    unsigned char* pix = (unsigned char*)calloc((size_t)(w * h * 4), 1);
    if (!pix) {
        fprintf(stderr, "[GL] 아틀라스 staging 메모리 부족\n");
        return 0;
    }
    int id = _next_atlas++;
    _atlases[id].w         = w;
    _atlases[id].h         = h;
    _atlases[id].pixels    = pix;
    _atlases[id].shelf_x   = 0;
    _atlases[id].shelf_y   = 0;
    _atlases[id].shelf_h   = 0;
    _atlases[id].finalized = 0;
    _atlases[id].tex_id    = 0;
    _atlases[id].valid     = 1;
    return id;
}

int gl_atlas_add(int atlas, const char* path) {
    if (atlas <= 0 || atlas >= _next_atlas || !_atlases[atlas].valid) return 0;
    _DnhAtlas* A = &_atlases[atlas];
    if (A->finalized) {
        fprintf(stderr, "[GL] 이미 finalize된 atlas — 추가 불가\n");
        return 0;
    }
    if (_next_sub >= _DNH_SUB_MAX) {
        fprintf(stderr, "[GL] sub-sprite 슬롯 초과\n");
        return 0;
    }
    SDL_Surface* raw = SDL_LoadBMP(path);
    if (!raw) {
        fprintf(stderr, "[GL atlas] SDL_LoadBMP (%s): %s\n", path, SDL_GetError());
        return 0;
    }
    SDL_Surface* rgba = SDL_ConvertSurfaceFormat(raw, SDL_PIXELFORMAT_ABGR8888, 0);
    SDL_FreeSurface(raw);
    if (!rgba) {
        fprintf(stderr, "[GL atlas] SDL_ConvertSurfaceFormat: %s\n", SDL_GetError());
        return 0;
    }
    int sw = rgba->w, sh = rgba->h;
    if (sw > A->w || sh > A->h) {
        fprintf(stderr, "[GL atlas] sprite (%dx%d) > atlas (%dx%d)\n", sw, sh, A->w, A->h);
        SDL_FreeSurface(rgba);
        return 0;
    }
    // shelf-fit
    if (A->shelf_x + sw > A->w) {
        A->shelf_x  = 0;
        A->shelf_y += A->shelf_h;
        A->shelf_h  = 0;
    }
    if (A->shelf_y + sh > A->h) {
        fprintf(stderr, "[GL atlas] 공간 부족 (shelf-fit)\n");
        SDL_FreeSurface(rgba);
        return 0;
    }
    int sx = A->shelf_x, sy = A->shelf_y;
    A->shelf_x += sw;
    if (sh > A->shelf_h) A->shelf_h = sh;

    // CPU staging에 픽셀 복사
    unsigned char* src = (unsigned char*)rgba->pixels;
    int src_pitch = rgba->pitch;
    int dst_pitch = A->w * 4;
    for (int row = 0; row < sh; row++) {
        memcpy(&A->pixels[(sy + row) * dst_pitch + sx * 4],
               &src[row * src_pitch],
               (size_t)(sw * 4));
    }
    SDL_FreeSurface(rgba);

    int sid = _next_sub++;
    _subs[sid].atlas_id = atlas;
    _subs[sid].x = sx; _subs[sid].y = sy;
    _subs[sid].w = sw; _subs[sid].h = sh;
    // UV는 finalize 시점에 계산 (atlas 크기 알 때) — 사실 지금도 가능
    _subs[sid].u0 = (double)sx / (double)A->w;
    _subs[sid].v0 = (double)sy / (double)A->h;
    _subs[sid].u1 = (double)(sx + sw) / (double)A->w;
    _subs[sid].v1 = (double)(sy + sh) / (double)A->h;
    return sid;
}

int gl_atlas_finalize(int atlas) {
    if (atlas <= 0 || atlas >= _next_atlas || !_atlases[atlas].valid) return 0;
    _DnhAtlas* A = &_atlases[atlas];
    if (A->finalized) return A->tex_id;
    if (_dnh_next_tex >= _DNH_TEX_MAX) {
        fprintf(stderr, "[GL atlas] 텍스처 슬롯 초과\n");
        return 0;
    }
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, A->w, A->h, 0, GL_RGBA, GL_UNSIGNED_BYTE, A->pixels);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    _dnh_apply_mipmap();

    int id = _dnh_next_tex++;
    _dnh_textures[id].tex   = tex;
    _dnh_textures[id].w     = A->w;
    _dnh_textures[id].h     = A->h;
    _dnh_textures[id].valid = 1;
    A->tex_id    = id;
    A->finalized = 1;
    free(A->pixels);
    A->pixels = NULL;
    return id;
}

// Sub-sprite 메타 조회 (모두 double — dh f64 ABI 안전)
int    gl_sub_tex(int sub)    { if (sub<=0||sub>=_next_sub) return 0; int a=_subs[sub].atlas_id; return _atlases[a].tex_id; }
int    gl_sub_w(int sub)      { if (sub<=0||sub>=_next_sub) return 0; return _subs[sub].w; }
int    gl_sub_h(int sub)      { if (sub<=0||sub>=_next_sub) return 0; return _subs[sub].h; }
double gl_sub_u0(int sub)     { if (sub<=0||sub>=_next_sub) return 0.0; return _subs[sub].u0; }
double gl_sub_v0(int sub)     { if (sub<=0||sub>=_next_sub) return 0.0; return _subs[sub].v0; }
double gl_sub_u1(int sub)     { if (sub<=0||sub>=_next_sub) return 1.0; return _subs[sub].u1; }
double gl_sub_v1(int sub)     { if (sub<=0||sub>=_next_sub) return 1.0; return _subs[sub].v1; }

// 배치에 sub-sprite 한 번에 추가 (atlas 텍스처 + UV 자동 적용)
void gl_sprite_add_sub(int sub, double x, double y, double w, double h,
                       double r, double g, double b, double a) {
    if (sub <= 0 || sub >= _next_sub) return;
    int atlas = _subs[sub].atlas_id;
    if (atlas <= 0 || atlas >= _next_atlas || !_atlases[atlas].finalized) return;
    gl_sprite_add(_atlases[atlas].tex_id, x, y, w, h,
                  _subs[sub].u0, _subs[sub].v0, _subs[sub].u1, _subs[sub].v1,
                  r, g, b, a);
}

// ---- Stage 79: 배치 스프라이트 렌더러 ----
// 같은 텍스처의 sprite 들을 한 번의 glDrawElements 호출로 묶어서 draw call 폭발 방지.
// vertex layout: pos(2f) + uv(2f) + color(4f) = 8 floats / vert, 4 verts / sprite.
// 텍스처가 바뀌면 자동 flush. batch 한도 도달해도 자동 flush.
//
// 사용 패턴:
//   gl_sprite_begin();
//   for (int i = 0; i < N; i++) gl_sprite_add(tex, x, y, w, h, 0,0,1,1, 1,1,1,1);
//   gl_sprite_end();

#define _DNH_BATCH_MAX 4096     // sprite 단위
static GLuint2 _sb_vbo = 0;
static GLuint2 _sb_ibo = 0;
static GLuint2 _sb_prog = 0;
static GLint2  _sb_loc_screen = -1;
static GLint2  _sb_loc_tex    = -1;
// CPU 버퍼: pos.x,pos.y, uv.x,uv.y, r,g,b,a (8 floats per vert) × 4 vert/sprite
static float   _sb_verts[_DNH_BATCH_MAX * 4 * 8];
static int     _sb_count = 0;       // 현재 누적된 sprite 수
static int     _sb_active = 0;      // begin과 end 사이인지
static int     _sb_cur_tex = -1;    // 현재 바인딩 텍스처 slot (-1=없음)
static int     _sb_cur_blend = 0;   // 0 = alpha, 1 = additive (glow)
static int     _sb_ready = 0;       // 일회성 init 완료 플래그
static int     _sb_init_failed = 0; // 초기화 영구 실패 플래그

static const char* _sb_vert_src =
    "attribute vec2 a_pos;\n"
    "attribute vec2 a_uv;\n"
    "attribute vec4 a_color;\n"
    "uniform vec2 u_screen;\n"
    "varying vec2 v_uv;\n"
    "varying vec4 v_color;\n"
    "void main() {\n"
    "    vec2 p = a_pos / u_screen * 2.0 - 1.0;\n"
    "    p.y = -p.y;\n"
    "    gl_Position = vec4(p, 0.0, 1.0);\n"
    "    v_uv = a_uv;\n"
    "    v_color = a_color;\n"
    "}\n";

static const char* _sb_frag_src =
    "uniform sampler2D u_tex;\n"
    "varying vec2 v_uv;\n"
    "varying vec4 v_color;\n"
    "void main() {\n"
    "    gl_FragColor = texture2D(u_tex, v_uv) * v_color;\n"
    "}\n";

static GLuint2 _sb_compile_shader(GLenum kind, const char* src) {
    GLuint2 s = _glCreateShader(kind);
    _glShaderSource(s, 1, &src, NULL);
    _glCompileShader(s);
    GLint2 ok = 0;
    _glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024]; _glGetShaderInfoLog(s, 1024, NULL, log);
        fprintf(stderr, "[GL] batch shader 컴파일 실패: %s\n", log);
        if (_glDeleteShader2) _glDeleteShader2(s);
        return 0;
    }
    return s;
}

static int _sb_init(void) {
    if (_sb_ready) return 1;
    if (_sb_init_failed) return 0;
    if (!_glCreateShader || !_glCreateProgram || !_glGenBuffers) {
        _load_gl2();
        if (!_glCreateShader || !_glCreateProgram || !_glGenBuffers) {
            fprintf(stderr, "[GL] batch: GL 2.0 확장 미지원\n");
            _sb_init_failed = 1;
            return 0;
        }
    }
    GLuint2 vs = _sb_compile_shader(GL_VERTEX_SHADER, _sb_vert_src);
    GLuint2 fs = _sb_compile_shader(GL_FRAGMENT_SHADER, _sb_frag_src);
    if (!vs || !fs) return 0;
    _sb_prog = _glCreateProgram();
    _glAttachShader(_sb_prog, vs);
    _glAttachShader(_sb_prog, fs);
    _glBindAttribLocation(_sb_prog, 0, "a_pos");
    _glBindAttribLocation(_sb_prog, 1, "a_uv");
    _glBindAttribLocation(_sb_prog, 2, "a_color");
    _glLinkProgram(_sb_prog);
    GLint2 ok = 0;
    _glGetProgramiv(_sb_prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        fprintf(stderr, "[GL] batch shader 링크 실패\n");
        return 0;
    }
    if (_glDeleteShader2) { _glDeleteShader2(vs); _glDeleteShader2(fs); }
    _sb_loc_screen = _glGetUniformLocation(_sb_prog, "u_screen");
    _sb_loc_tex    = _glGetUniformLocation(_sb_prog, "u_tex");

    _glGenBuffers(1, &_sb_vbo);
    _glBindBuffer(GL_ARRAY_BUFFER, _sb_vbo);
    _glBufferData(GL_ARRAY_BUFFER,
        (GLsizeiptr2)(sizeof(_sb_verts)), NULL, GL_DYNAMIC_DRAW);

    // 인덱스 버퍼: 각 sprite마다 2 삼각형 = 6 인덱스. 4 vert pattern: 0,1,2, 2,3,0.
    _glGenBuffers(1, &_sb_ibo);
    _glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, _sb_ibo);
    unsigned int* idx = (unsigned int*)malloc(sizeof(unsigned int) * _DNH_BATCH_MAX * 6);
    if (!idx) {
        fprintf(stderr, "[GL] batch index buffer malloc 실패\n");
        return 0;
    }
    for (int i = 0; i < _DNH_BATCH_MAX; i++) {
        unsigned int base = (unsigned int)i * 4;
        idx[i*6+0] = base + 0;
        idx[i*6+1] = base + 1;
        idx[i*6+2] = base + 2;
        idx[i*6+3] = base + 2;
        idx[i*6+4] = base + 3;
        idx[i*6+5] = base + 0;
    }
    _glBufferData(GL_ELEMENT_ARRAY_BUFFER,
        (GLsizeiptr2)(sizeof(unsigned int) * _DNH_BATCH_MAX * 6),
        idx, GL_STATIC_DRAW);
    free(idx);

    _glBindBuffer(GL_ARRAY_BUFFER, 0);
    _glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0);
    _sb_ready = 1;
    return 1;
}

static void _sb_apply_blend(int mode) {
    // 0 = 일반 알파, 1 = additive (glow). src*src_alpha + dst.
    if (mode == 1) {
        glBlendFunc(GL_SRC_ALPHA, GL_ONE);
    } else {
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    }
}

static void _sb_flush(void) {
    if (_sb_count <= 0 || _sb_cur_tex < 0) { _sb_count = 0; return; }
    int n_verts = _sb_count * 4;
    int n_idx   = _sb_count * 6;

    _sb_apply_blend(_sb_cur_blend);

    _glBindBuffer(GL_ARRAY_BUFFER, _sb_vbo);
    _glBufferSubData(GL_ARRAY_BUFFER, 0,
        (GLsizeiptr2)(sizeof(float) * n_verts * 8), _sb_verts);
    _glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, _sb_ibo);

    // attribute pointers (stride = 32 bytes)
    _glEnableVertexAttribArray(0);
    _glVertexAttribPointer(0, 2, GL_FLOAT, 0, 32, (const void*)0);
    _glEnableVertexAttribArray(1);
    _glVertexAttribPointer(1, 2, GL_FLOAT, 0, 32, (const void*)(2 * sizeof(float)));
    _glEnableVertexAttribArray(2);
    _glVertexAttribPointer(2, 4, GL_FLOAT, 0, 32, (const void*)(4 * sizeof(float)));

    glEnable(GL_TEXTURE_2D);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _dnh_textures[_sb_cur_tex].tex);

    glDrawElements(GL_TRIANGLES, n_idx, GL_UNSIGNED_INT, (const void*)0);

    _glDisableVertexAttribArray(0);
    _glDisableVertexAttribArray(1);
    _glDisableVertexAttribArray(2);
    _glBindBuffer(GL_ARRAY_BUFFER, 0);
    _glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0);
    _sb_count = 0;
}

void gl_sprite_begin(void) {
    if (!_sb_init()) return;
    _glUseProgram(_sb_prog);
    if (_sb_loc_screen >= 0 && _glUniform2f) _glUniform2f(_sb_loc_screen, (float)_gl_w, (float)_gl_h);
    if (_sb_loc_tex    >= 0 && _glUniform1i) _glUniform1i(_sb_loc_tex, 0);
    _sb_count = 0;
    _sb_cur_tex = -1;
    _sb_cur_blend = 0;
    _sb_active = 1;
}

// Stage 84: blend mode 토글. begin/end 사이에 호출 가능 — 자동 flush 후 mode 바꿈.
// 0 = alpha (기본), 1 = additive (glow/입자 합산).
void gl_sprite_blend(int mode) {
    if (!_sb_active) return;
    if (mode == _sb_cur_blend) return;
    _sb_flush();
    _sb_cur_blend = mode;
}

void gl_sprite_add(int tex, double x, double y, double w, double h,
                   double u0, double v0, double u1, double v1,
                   double r, double g, double b, double a) {
    if (!_sb_active) return;
    if (tex <= 0 || tex >= _dnh_next_tex || !_dnh_textures[tex].valid) return;
    if (tex != _sb_cur_tex) {
        _sb_flush();
        _sb_cur_tex = tex;
    }
    if (_sb_count >= _DNH_BATCH_MAX) {
        _sb_flush();
        _sb_cur_tex = tex;
    }
    float fx=(float)x, fy=(float)y, fw=(float)w, fh=(float)h;
    float fu0=(float)u0, fv0=(float)v0, fu1=(float)u1, fv1=(float)v1;
    float fr=(float)r, fg=(float)g, fb=(float)b, fa=(float)a;
    float* v = &_sb_verts[_sb_count * 4 * 8];
    v[0]=fx;     v[1]=fy;     v[2]=fu0; v[3]=fv0; v[4]=fr; v[5]=fg; v[6]=fb; v[7]=fa;
    v[8]=fx+fw;  v[9]=fy;     v[10]=fu1; v[11]=fv0; v[12]=fr; v[13]=fg; v[14]=fb; v[15]=fa;
    v[16]=fx+fw; v[17]=fy+fh; v[18]=fu1; v[19]=fv1; v[20]=fr; v[21]=fg; v[22]=fb; v[23]=fa;
    v[24]=fx;    v[25]=fy+fh; v[26]=fu0; v[27]=fv1; v[28]=fr; v[29]=fg; v[30]=fb; v[31]=fa;
    _sb_count++;
}

void gl_sprite_end(void) {
    if (!_sb_active) return;
    _sb_flush();
    if (_glUseProgram) _glUseProgram(0);
    glDisable(GL_TEXTURE_2D);
    // blend mode 기본으로 복귀
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    _sb_active = 0;
    _sb_cur_tex = -1;
    _sb_cur_blend = 0;
}

// Stage 84: 회전 sprite — 중심(cx,cy)에 크기(w,h), 라디안 angle.
// quad 4점을 sin/cos로 회전 변환해서 batch에 add. 4점은 정렬 안 됐어도 batch IBO 패턴(0,1,2,2,3,0)이 동일하게 작동.
void gl_sprite_add_rot(int tex, double cx, double cy, double w, double h, double angle,
                       double u0, double v0, double u1, double v1,
                       double r, double g, double b, double a) {
    if (!_sb_active) return;
    if (tex <= 0 || tex >= _dnh_next_tex || !_dnh_textures[tex].valid) return;
    if (tex != _sb_cur_tex) { _sb_flush(); _sb_cur_tex = tex; }
    if (_sb_count >= _DNH_BATCH_MAX) { _sb_flush(); _sb_cur_tex = tex; }

    double ca = cos(angle), sa = sin(angle);
    double hw = w * 0.5, hh = h * 0.5;
    // 회전 전 corner: (-hw,-hh) (+hw,-hh) (+hw,+hh) (-hw,+hh)
    double x0r = -hw * ca - (-hh) * sa, y0r = -hw * sa + (-hh) * ca;
    double x1r =  hw * ca - (-hh) * sa, y1r =  hw * sa + (-hh) * ca;
    double x2r =  hw * ca -   hh  * sa, y2r =  hw * sa +   hh  * ca;
    double x3r = -hw * ca -   hh  * sa, y3r = -hw * sa +   hh  * ca;
    float fcx=(float)cx, fcy=(float)cy;
    float fu0=(float)u0, fv0=(float)v0, fu1=(float)u1, fv1=(float)v1;
    float fr=(float)r, fg=(float)g, fb=(float)b, fa=(float)a;
    float* v = &_sb_verts[_sb_count * 4 * 8];
    v[0]  = fcx + (float)x0r; v[1]  = fcy + (float)y0r; v[2]  = fu0; v[3]  = fv0; v[4]=fr; v[5]=fg; v[6]=fb; v[7]=fa;
    v[8]  = fcx + (float)x1r; v[9]  = fcy + (float)y1r; v[10] = fu1; v[11] = fv0; v[12]=fr; v[13]=fg; v[14]=fb; v[15]=fa;
    v[16] = fcx + (float)x2r; v[17] = fcy + (float)y2r; v[18] = fu1; v[19] = fv1; v[20]=fr; v[21]=fg; v[22]=fb; v[23]=fa;
    v[24] = fcx + (float)x3r; v[25] = fcy + (float)y3r; v[26] = fu0; v[27] = fv1; v[28]=fr; v[29]=fg; v[30]=fb; v[31]=fa;
    _sb_count++;
}

// Stage 84: 소프트 그라데이션 원형 텍스처 — 중심 1.0, 가장자리 0.0 라디얼 폴오프.
// 입자/광원/총탄에 sharp quad 대신 부드러운 광점. cached: 같은 size 호출은 같은 id.
// pow_falloff: 1.0 = 선형, 2.0 = 좀 더 sharp 중심, 0.5 = soft.
int gl_texture_soft_circle(int size, double pow_falloff) {
    if (size < 2) size = 2;
    if (size > 512) size = 512;
    if (_dnh_next_tex >= _DNH_TEX_MAX) return 0;
    unsigned char* pix = (unsigned char*)malloc((size_t)(size * size * 4));
    if (!pix) return 0;
    double cx = (size - 1) * 0.5, cy = (size - 1) * 0.5;
    double rmax = cx;
    for (int y = 0; y < size; y++) {
        for (int x = 0; x < size; x++) {
            double dx = x - cx, dy = y - cy;
            double d = sqrt(dx*dx + dy*dy) / rmax;
            if (d > 1.0) d = 1.0;
            double f = 1.0 - d;
            if (pow_falloff != 1.0) f = pow(f, pow_falloff);
            unsigned char A = (unsigned char)(f * 255.0 + 0.5);
            int o = (y * size + x) * 4;
            pix[o+0] = 255; pix[o+1] = 255; pix[o+2] = 255; pix[o+3] = A;
        }
    }
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, size, size, 0, GL_RGBA, GL_UNSIGNED_BYTE, pix);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    _dnh_apply_mipmap();
    free(pix);
    int id = _dnh_next_tex++;
    _dnh_textures[id].tex   = tex;
    _dnh_textures[id].w     = size;
    _dnh_textures[id].h     = size;
    _dnh_textures[id].valid = 1;
    return id;
}

// ---- 셰이더 ----

int gl_shader_new(const char* vert_src, const char* frag_src) {
    if (!_glCreateShader || !_glCreateProgram) return 0;
    GLint2 ok;

    GLuint2 vs = _glCreateShader(GL_VERTEX_SHADER);
    _glShaderSource(vs, 1, &vert_src, NULL);
    _glCompileShader(vs);
    _glGetShaderiv(vs, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512]; _glGetShaderInfoLog(vs, 512, NULL, log);
        fprintf(stderr, "[GL] vert shader 오류: %s\n", log);
        if (_glDeleteShader2) _glDeleteShader2(vs);
        return 0;
    }

    GLuint2 fs = _glCreateShader(GL_FRAGMENT_SHADER);
    _glShaderSource(fs, 1, &frag_src, NULL);
    _glCompileShader(fs);
    _glGetShaderiv(fs, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512]; _glGetShaderInfoLog(fs, 512, NULL, log);
        fprintf(stderr, "[GL] frag shader 오류: %s\n", log);
        if (_glDeleteShader2) { _glDeleteShader2(vs); _glDeleteShader2(fs); }
        return 0;
    }

    GLuint2 prog = _glCreateProgram();
    _glAttachShader(prog, vs);
    _glAttachShader(prog, fs);
    _glLinkProgram(prog);
    _glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) { fprintf(stderr, "[GL] 셰이더 링크 실패\n"); return 0; }

    if (_glDeleteShader2) { _glDeleteShader2(vs); _glDeleteShader2(fs); }
    return (int)prog;
}

void gl_shader_use(int prog)  { if (_glUseProgram) _glUseProgram((GLuint2)prog); }
void gl_shader_off(void)      { if (_glUseProgram) _glUseProgram(0); }

void gl_set_float(int prog, const char* name, float v) {
    if (!_glGetUniformLocation) return;
    GLint2 loc = _glGetUniformLocation((GLuint2)prog, name);
    if (loc >= 0 && _glUniform1f) _glUniform1f(loc, v);
}
void gl_set_vec2(int prog, const char* name, float x, float y) {
    if (!_glGetUniformLocation) return;
    GLint2 loc = _glGetUniformLocation((GLuint2)prog, name);
    if (loc >= 0 && _glUniform2f) _glUniform2f(loc, x, y);
}
void gl_set_vec3(int prog, const char* name, float x, float y, float z) {
    if (!_glGetUniformLocation) return;
    GLint2 loc = _glGetUniformLocation((GLuint2)prog, name);
    if (loc >= 0 && _glUniform3f) _glUniform3f(loc, x, y, z);
}
void gl_set_vec4(int prog, const char* name, float x, float y, float z, float w) {
    if (!_glGetUniformLocation) return;
    GLint2 loc = _glGetUniformLocation((GLuint2)prog, name);
    if (loc >= 0 && _glUniform4f) _glUniform4f(loc, x, y, z, w);
}
void gl_set_int(int prog, const char* name, int v) {
    if (!_glGetUniformLocation) return;
    GLint2 loc = _glGetUniformLocation((GLuint2)prog, name);
    if (loc >= 0 && _glUniform1i) _glUniform1i(loc, v);
}

// ---- 입력 ----

int  gl_key(int sdl_scancode) {
    const Uint8* ks = SDL_GetKeyboardState(NULL);
    return (sdl_scancode >= 0 && sdl_scancode < 512) ? ks[sdl_scancode] : 0;
}
int  gl_mouse_x(void)          { int x; SDL_GetMouseState(&x, NULL); return x; }
int  gl_mouse_y(void)          { int y; SDL_GetMouseState(NULL, &y); return y; }
int  gl_mouse_button(int btn)  { return (SDL_GetMouseState(NULL,NULL) & SDL_BUTTON(btn)) ? 1 : 0; }
void gl_delay(int ms)          { SDL_Delay((Uint32)ms); }
double gl_ticks(void)          { return SDL_GetTicks() / 1000.0; }
// Stage 79: ABI 안전 (i32 ABI) ms 시각 — 데모/벤치용
int gl_ticks_ms(void)          { return (int)SDL_GetTicks(); }
// Stage 79: vsync on(1)/off(0). 벤치는 0으로 진짜 처리량 측정.
void gl_vsync(int on)          { SDL_GL_SetSwapInterval(on ? 1 : 0); }

// Stage 79: BMP 없이도 batch 테스트 가능하도록 1x1 흰 텍스처 캐시.
// 같은 호출은 같은 id 반환. tint로 색깔 입혀 다양한 quad 그리기 가능.
int gl_texture_white(void) {
    static int cached = 0;
    if (cached > 0) return cached;
    if (_dnh_next_tex >= _DNH_TEX_MAX) return 0;
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    unsigned char pix[4] = {255, 255, 255, 255};
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 1, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, pix);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    int id = _dnh_next_tex++;
    _dnh_textures[id].tex   = tex;
    _dnh_textures[id].w     = 1;
    _dnh_textures[id].h     = 1;
    _dnh_textures[id].valid = 1;
    cached = id;
    return id;
}

// ============================================================
// Stage 87: Bloom 후처리 (FBO + 가우시안 blur + composite)
// ============================================================
//
// 흐름:
//   gl_bloom_init()          한 번. 윈도우 크기로 3 FBO + 3 셰이더 생성.
//   매 프레임:
//     gl_bloom_begin()       이후 그리기를 scene FBO에 redirect
//     gl_clear(...); sprite_batch...; gl_text_draw...
//     gl_bloom_end()         bright pass → blur H → blur V → composite → backbuffer
//     gl_swap()
//
// 파이프라인 (모두 풀스크린 quad pass):
//   1. scene_fbo: 사용자 콘텐츠 렌더링 대상
//   2. bloom_a_fbo: bright pass — luminance > threshold만 통과
//   3. bloom_b_fbo: horizontal Gaussian blur
//   4. bloom_a_fbo: vertical Gaussian blur (재사용)
//   5. backbuffer:  composite — scene + bloom_a * intensity

static GLuint _bl_scene_fbo = 0, _bl_scene_tex = 0;
static GLuint _bl_a_fbo     = 0, _bl_a_tex     = 0;
static GLuint _bl_b_fbo     = 0, _bl_b_tex     = 0;
static GLuint _bl_prog_bright    = 0;
static GLuint _bl_prog_blur      = 0;
static GLuint _bl_prog_composite = 0;
static GLint  _bl_loc_bright_tex = -1, _bl_loc_bright_thr = -1;
static GLint  _bl_loc_blur_tex = -1, _bl_loc_blur_dir = -1;
static GLint  _bl_loc_comp_scene = -1, _bl_loc_comp_blur = -1, _bl_loc_comp_int = -1;
static GLuint _bl_fsq_vbo = 0;
static int    _bl_w = 0, _bl_h = 0;
static int    _bl_ready = 0;
static int    _bl_active = 0;
static float  _bl_threshold = 0.55f;
static float  _bl_intensity = 1.4f;

static const char* _bl_vert_src =
    "attribute vec2 a_pos;\n"
    "attribute vec2 a_uv;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "    v_uv = a_uv;\n"
    "}\n";

static const char* _bl_bright_frag =
    "uniform sampler2D u_tex;\n"
    "uniform float u_threshold;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    vec4 c = texture2D(u_tex, v_uv);\n"
    "    float l = dot(c.rgb, vec3(0.299, 0.587, 0.114));\n"
    "    float m = max(0.0, l - u_threshold) / max(0.001, 1.0 - u_threshold);\n"
    "    gl_FragColor = vec4(c.rgb * m, 1.0);\n"
    "}\n";

static const char* _bl_blur_frag =
    "uniform sampler2D u_tex;\n"
    "uniform vec2 u_dir;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    vec4 s = vec4(0.0);\n"
    "    s += texture2D(u_tex, v_uv - u_dir * 4.0) * 0.0162;\n"
    "    s += texture2D(u_tex, v_uv - u_dir * 3.0) * 0.0540;\n"
    "    s += texture2D(u_tex, v_uv - u_dir * 2.0) * 0.1216;\n"
    "    s += texture2D(u_tex, v_uv - u_dir * 1.0) * 0.1945;\n"
    "    s += texture2D(u_tex, v_uv                ) * 0.2270;\n"
    "    s += texture2D(u_tex, v_uv + u_dir * 1.0) * 0.1945;\n"
    "    s += texture2D(u_tex, v_uv + u_dir * 2.0) * 0.1216;\n"
    "    s += texture2D(u_tex, v_uv + u_dir * 3.0) * 0.0540;\n"
    "    s += texture2D(u_tex, v_uv + u_dir * 4.0) * 0.0162;\n"
    "    gl_FragColor = s;\n"
    "}\n";

static const char* _bl_composite_frag =
    "uniform sampler2D u_scene;\n"
    "uniform sampler2D u_blur;\n"
    "uniform float u_intensity;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    vec4 sc = texture2D(u_scene, v_uv);\n"
    "    vec4 bl = texture2D(u_blur,  v_uv);\n"
    "    gl_FragColor = vec4(sc.rgb + bl.rgb * u_intensity, sc.a);\n"
    "}\n";

static GLuint _bl_compile(GLenum kind, const char* src) {
    GLuint2 s = _glCreateShader(kind);
    _glShaderSource(s, 1, &src, NULL);
    _glCompileShader(s);
    GLint2 ok = 0;
    _glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024]; _glGetShaderInfoLog(s, 1024, NULL, log);
        fprintf(stderr, "[bloom] shader 컴파일 실패: %s\n", log);
        if (_glDeleteShader2) _glDeleteShader2(s);
        return 0;
    }
    return s;
}

static GLuint _bl_link(const char* fsrc) {
    GLuint vs = _bl_compile(GL_VERTEX_SHADER, _bl_vert_src);
    GLuint fs = _bl_compile(GL_FRAGMENT_SHADER, fsrc);
    if (!vs || !fs) return 0;
    GLuint p = _glCreateProgram();
    _glAttachShader(p, vs);
    _glAttachShader(p, fs);
    _glBindAttribLocation(p, 0, "a_pos");
    _glBindAttribLocation(p, 1, "a_uv");
    _glLinkProgram(p);
    GLint2 ok = 0;
    _glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) { fprintf(stderr, "[bloom] 링크 실패\n"); return 0; }
    if (_glDeleteShader2) { _glDeleteShader2(vs); _glDeleteShader2(fs); }
    return p;
}

// 색상 텍스처 한 장 + FBO 한 장 짝지어 생성
static int _bl_make_fbo(int w, int h, GLuint* out_fbo, GLuint* out_tex) {
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    GLuint fbo = 0;
    _glGenFramebuffers(1, &fbo);
    _glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    _glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    GLenum st = _glCheckFramebufferStatus(GL_FRAMEBUFFER);
    if (st != GL_FRAMEBUFFER_COMPLETE) {
        fprintf(stderr, "[bloom] FBO incomplete (status %x)\n", st);
        return 0;
    }
    *out_fbo = fbo;
    *out_tex = tex;
    return 1;
}

int gl_bloom_init(void) {
    if (_bl_ready) return 1;
    if (!_glGenFramebuffers || !_glCreateShader) {
        fprintf(stderr, "[bloom] FBO 또는 shader 지원 없음\n");
        return 0;
    }
    _bl_w = _gl_w;
    _bl_h = _gl_h;
    if (!_bl_make_fbo(_bl_w, _bl_h, &_bl_scene_fbo, &_bl_scene_tex)) return 0;
    if (!_bl_make_fbo(_bl_w, _bl_h, &_bl_a_fbo, &_bl_a_tex)) return 0;
    if (!_bl_make_fbo(_bl_w, _bl_h, &_bl_b_fbo, &_bl_b_tex)) return 0;
    _glBindFramebuffer(GL_FRAMEBUFFER, 0);

    _bl_prog_bright    = _bl_link(_bl_bright_frag);
    _bl_prog_blur      = _bl_link(_bl_blur_frag);
    _bl_prog_composite = _bl_link(_bl_composite_frag);
    if (!_bl_prog_bright || !_bl_prog_blur || !_bl_prog_composite) return 0;

    _bl_loc_bright_tex = _glGetUniformLocation(_bl_prog_bright, "u_tex");
    _bl_loc_bright_thr = _glGetUniformLocation(_bl_prog_bright, "u_threshold");
    _bl_loc_blur_tex   = _glGetUniformLocation(_bl_prog_blur,   "u_tex");
    _bl_loc_blur_dir   = _glGetUniformLocation(_bl_prog_blur,   "u_dir");
    _bl_loc_comp_scene = _glGetUniformLocation(_bl_prog_composite, "u_scene");
    _bl_loc_comp_blur  = _glGetUniformLocation(_bl_prog_composite, "u_blur");
    _bl_loc_comp_int   = _glGetUniformLocation(_bl_prog_composite, "u_intensity");

    // 풀스크린 quad (NDC): pos.x, pos.y, uv.x, uv.y × 4 vert (TRIANGLE_STRIP)
    float fsq[16] = {
        -1.0f, -1.0f,  0.0f, 0.0f,
         1.0f, -1.0f,  1.0f, 0.0f,
        -1.0f,  1.0f,  0.0f, 1.0f,
         1.0f,  1.0f,  1.0f, 1.0f,
    };
    _glGenBuffers(1, &_bl_fsq_vbo);
    _glBindBuffer(GL_ARRAY_BUFFER, _bl_fsq_vbo);
    _glBufferData(GL_ARRAY_BUFFER, sizeof(fsq), fsq, GL_STATIC_DRAW);
    _glBindBuffer(GL_ARRAY_BUFFER, 0);

    _bl_ready = 1;
    return 1;
}

void gl_bloom_set(double threshold, double intensity) {
    _bl_threshold = (float)threshold;
    _bl_intensity = (float)intensity;
}

void gl_bloom_begin(void) {
    if (!_bl_ready) { if (!gl_bloom_init()) return; }
    _glBindFramebuffer(GL_FRAMEBUFFER, _bl_scene_fbo);
    glViewport(0, 0, _bl_w, _bl_h);
    _bl_active = 1;
}

static void _bl_draw_fsq(GLuint prog) {
    _glUseProgram(prog);
    _glBindBuffer(GL_ARRAY_BUFFER, _bl_fsq_vbo);
    _glEnableVertexAttribArray(0);
    _glVertexAttribPointer(0, 2, GL_FLOAT, 0, 16, (const void*)0);
    _glEnableVertexAttribArray(1);
    _glVertexAttribPointer(1, 2, GL_FLOAT, 0, 16, (const void*)(2 * sizeof(float)));
    glDisable(GL_BLEND);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    _glDisableVertexAttribArray(0);
    _glDisableVertexAttribArray(1);
}

void gl_bloom_end(void) {
    if (!_bl_active) return;
    _bl_active = 0;

    // 1) Bright pass: scene → A
    _glBindFramebuffer(GL_FRAMEBUFFER, _bl_a_fbo);
    glViewport(0, 0, _bl_w, _bl_h);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _bl_scene_tex);
    if (_bl_loc_bright_tex >= 0) _glUniform1i(_bl_loc_bright_tex, 0);
    _glUseProgram(_bl_prog_bright);
    if (_bl_loc_bright_thr >= 0) _glUniform1f(_bl_loc_bright_thr, _bl_threshold);
    _bl_draw_fsq(_bl_prog_bright);

    // 2) Blur H: A → B
    _glBindFramebuffer(GL_FRAMEBUFFER, _bl_b_fbo);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _bl_a_tex);
    _glUseProgram(_bl_prog_blur);
    if (_bl_loc_blur_tex >= 0) _glUniform1i(_bl_loc_blur_tex, 0);
    if (_bl_loc_blur_dir >= 0) _glUniform2f(_bl_loc_blur_dir, 1.0f / (float)_bl_w, 0.0f);
    _bl_draw_fsq(_bl_prog_blur);

    // 3) Blur V: B → A (재사용)
    _glBindFramebuffer(GL_FRAMEBUFFER, _bl_a_fbo);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _bl_b_tex);
    if (_bl_loc_blur_dir >= 0) _glUniform2f(_bl_loc_blur_dir, 0.0f, 1.0f / (float)_bl_h);
    _bl_draw_fsq(_bl_prog_blur);

    // 4) Composite: scene + A → backbuffer
    _glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glViewport(0, 0, _gl_w, _gl_h);
    _glUseProgram(_bl_prog_composite);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _bl_scene_tex);
    if (_bl_loc_comp_scene >= 0) _glUniform1i(_bl_loc_comp_scene, 0);
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0 + 1);
    glBindTexture(GL_TEXTURE_2D, _bl_a_tex);
    if (_bl_loc_comp_blur >= 0) _glUniform1i(_bl_loc_comp_blur, 1);
    if (_bl_loc_comp_int >= 0) _glUniform1f(_bl_loc_comp_int, _bl_intensity);
    _bl_draw_fsq(_bl_prog_composite);

    // unit 0으로 복귀
    if (_glActiveTexture) _glActiveTexture(GL_TEXTURE0);
    _glUseProgram(0);
}

void dnh_gl_load_pointers(void) {
    _load_gl2();
    if (_glCreateShader && _glCreateProgram && _glGenBuffers) {
        _sb_init_failed = 0;
    }
}
