// danha_audio.c — 단아 오디오 래퍼 (Stage 80)
// SDL2 native audio. miniaudio/SDL_mixer 같은 추가 의존 없음.
// wav 로드 + 자체 voice pool 믹싱 + master volume.
//
// API:
//   audio_init()              디바이스 열기
//   audio_close()             종료
//   sound_load(path)          wav → slot id (>0). 자동 spec 변환.
//   sound_play(id)            놀고 있는 voice에 재생. 반환 voice slot (-1 실패)
//   sound_play_loop(id, n)    n회 반복 (n=-1 무한)
//   voice_stop(slot)          개별 voice 정지
//   sound_stop_all()          모든 voice 정지
//   sound_volume(slot, v)     활성 voice의 볼륨 (0~1)
//   master_volume(v)          전역 볼륨

#include <SDL2/SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define _DNH_SOUND_MAX 256
#define _DNH_VOICE_MAX 32

typedef struct {
    Uint8* buf;
    Uint32 len;     // bytes (post-convert)
    int    valid;
} _DnhSound;

typedef struct {
    int    sound_id;   // 0 = inactive
    Uint32 pos;        // 현재 프레임 위치
    int    loops;      // 남은 반복 (-1 = ∞, 0 = no loop)
    float  volume;     // voice별 게인 (0~1)
} _DnhVoice;

static SDL_AudioDeviceID _audio_dev   = 0;
static SDL_AudioSpec     _audio_spec;
static int               _audio_inited = 0;
static _DnhSound         _sounds[_DNH_SOUND_MAX];
static int               _next_sound  = 1;
static _DnhVoice         _voices[_DNH_VOICE_MAX];
static float             _master_vol  = 1.0f;

// SDL이 부르는 콜백. 출력 buffer를 0으로 채우고 활성 voice들을 합성.
// 모든 voice는 출력 spec (S16LSB, stereo, 44.1k 가정)에 이미 변환됨.
static void _audio_callback(void* userdata, Uint8* stream, int len) {
    (void)userdata;
    SDL_memset(stream, 0, (size_t)len);
    int16_t* out = (int16_t*)stream;
    int frames = len / (2 * 2);   // 2 bytes/sample × 2 channels

    for (int v = 0; v < _DNH_VOICE_MAX; v++) {
        _DnhVoice* voice = &_voices[v];
        if (voice->sound_id == 0) continue;
        _DnhSound* snd = &_sounds[voice->sound_id];
        if (!snd->valid) { voice->sound_id = 0; continue; }
        int16_t* src = (int16_t*)snd->buf;
        Uint32   src_frames = snd->len / 4;   // S16 stereo
        float    vol = voice->volume * _master_vol;

        for (int f = 0; f < frames; f++) {
            if (voice->pos >= src_frames) {
                if (voice->loops != 0) {
                    if (voice->loops > 0) voice->loops--;
                    voice->pos = 0;
                } else {
                    voice->sound_id = 0;
                    break;
                }
            }
            int32_t sL = (int32_t)((float)src[voice->pos * 2]     * vol);
            int32_t sR = (int32_t)((float)src[voice->pos * 2 + 1] * vol);
            int32_t mL = (int32_t)out[f * 2]     + sL;
            int32_t mR = (int32_t)out[f * 2 + 1] + sR;
            if (mL >  32767) mL =  32767;
            if (mL < -32768) mL = -32768;
            if (mR >  32767) mR =  32767;
            if (mR < -32768) mR = -32768;
            out[f * 2]     = (int16_t)mL;
            out[f * 2 + 1] = (int16_t)mR;
            voice->pos++;
        }
    }
}

int audio_init(void) {
    if (_audio_inited) return 1;
    if (SDL_WasInit(SDL_INIT_AUDIO) == 0) {
        if (SDL_InitSubSystem(SDL_INIT_AUDIO) != 0) {
            fprintf(stderr, "[audio] SDL_InitSubSystem: %s\n", SDL_GetError());
            return 0;
        }
    }
    SDL_AudioSpec want;
    SDL_memset(&want, 0, sizeof(want));
    want.freq     = 44100;
    want.format   = AUDIO_S16LSB;
    want.channels = 2;
    want.samples  = 2048;
    want.callback = _audio_callback;
    _audio_dev = SDL_OpenAudioDevice(NULL, 0, &want, &_audio_spec, 0);
    if (_audio_dev == 0) {
        fprintf(stderr, "[audio] OpenAudioDevice: %s\n", SDL_GetError());
        return 0;
    }
    SDL_memset(_voices, 0, sizeof(_voices));
    SDL_PauseAudioDevice(_audio_dev, 0);
    _audio_inited = 1;
    return 1;
}

void audio_close(void) {
    if (!_audio_inited) return;
    SDL_PauseAudioDevice(_audio_dev, 1);
    SDL_CloseAudioDevice(_audio_dev);
    _audio_dev = 0;
    for (int i = 1; i < _next_sound; i++) {
        if (_sounds[i].valid && _sounds[i].buf) {
            SDL_free(_sounds[i].buf);
            _sounds[i].buf = NULL;
            _sounds[i].valid = 0;
        }
    }
    _next_sound = 1;
    SDL_QuitSubSystem(SDL_INIT_AUDIO);
    _audio_inited = 0;
}

int sound_load(const char* path) {
    if (!_audio_inited) {
        fprintf(stderr, "[audio] sound_load 전에 audio_init() 필요\n");
        return 0;
    }
    if (_next_sound >= _DNH_SOUND_MAX) {
        fprintf(stderr, "[audio] 사운드 슬롯 초과 (max %d)\n", _DNH_SOUND_MAX);
        return 0;
    }
    SDL_AudioSpec wav_spec;
    Uint8* wav_buf = NULL;
    Uint32 wav_len = 0;
    if (SDL_LoadWAV(path, &wav_spec, &wav_buf, &wav_len) == NULL) {
        fprintf(stderr, "[audio] SDL_LoadWAV (%s): %s\n", path, SDL_GetError());
        return 0;
    }
    // 출력 spec과 다르면 자동 변환 (freq/format/channels)
    if (wav_spec.freq     != _audio_spec.freq     ||
        wav_spec.format   != _audio_spec.format   ||
        wav_spec.channels != _audio_spec.channels) {
        SDL_AudioCVT cvt;
        SDL_memset(&cvt, 0, sizeof(cvt));
        int needed = SDL_BuildAudioCVT(&cvt,
            wav_spec.format,    wav_spec.channels,    wav_spec.freq,
            _audio_spec.format, _audio_spec.channels, _audio_spec.freq);
        if (needed < 0) {
            fprintf(stderr, "[audio] SDL_BuildAudioCVT: %s\n", SDL_GetError());
            SDL_FreeWAV(wav_buf);
            return 0;
        }
        if (needed) {
            cvt.len = (int)wav_len;
            cvt.buf = (Uint8*)SDL_malloc((size_t)(cvt.len * cvt.len_mult));
            if (!cvt.buf) {
                SDL_FreeWAV(wav_buf);
                return 0;
            }
            SDL_memcpy(cvt.buf, wav_buf, wav_len);
            if (SDL_ConvertAudio(&cvt) < 0) {
                fprintf(stderr, "[audio] SDL_ConvertAudio: %s\n", SDL_GetError());
                SDL_free(cvt.buf);
                SDL_FreeWAV(wav_buf);
                return 0;
            }
            SDL_FreeWAV(wav_buf);
            wav_buf = cvt.buf;
            wav_len = (Uint32)cvt.len_cvt;
        }
    } else {
        // spec 일치하면 SDL_FreeWAV로 풀 수 있는 buf 그대로 사용
        // 단 우리가 audio_close에서 SDL_free로 풀 거라 SDL_malloc 영역으로 옮기지 않음
        // SDL_LoadWAV는 SDL_malloc 사용하므로 SDL_free 호환
    }
    int id = _next_sound++;
    _sounds[id].buf   = wav_buf;
    _sounds[id].len   = wav_len;
    _sounds[id].valid = 1;
    return id;
}

static int _alloc_voice(int sound_id, int loops) {
    SDL_LockAudioDevice(_audio_dev);
    int slot = -1;
    for (int v = 0; v < _DNH_VOICE_MAX; v++) {
        if (_voices[v].sound_id == 0) { slot = v; break; }
    }
    if (slot >= 0) {
        _voices[slot].sound_id = sound_id;
        _voices[slot].pos      = 0;
        _voices[slot].loops    = loops;
        _voices[slot].volume   = 1.0f;
    }
    SDL_UnlockAudioDevice(_audio_dev);
    return slot;
}

int sound_play(int id) {
    if (!_audio_inited || id <= 0 || id >= _next_sound || !_sounds[id].valid) return -1;
    return _alloc_voice(id, 0);
}

int sound_play_loop(int id, int loops) {
    if (!_audio_inited || id <= 0 || id >= _next_sound || !_sounds[id].valid) return -1;
    return _alloc_voice(id, loops);
}

void voice_stop(int slot) {
    if (!_audio_inited) return;
    if (slot < 0 || slot >= _DNH_VOICE_MAX) return;
    SDL_LockAudioDevice(_audio_dev);
    _voices[slot].sound_id = 0;
    SDL_UnlockAudioDevice(_audio_dev);
}

void voice_volume(int slot, double vol) {
    if (!_audio_inited) return;
    if (slot < 0 || slot >= _DNH_VOICE_MAX) return;
    if (vol < 0) vol = 0; if (vol > 1) vol = 1;
    SDL_LockAudioDevice(_audio_dev);
    _voices[slot].volume = (float)vol;
    SDL_UnlockAudioDevice(_audio_dev);
}

void sound_stop_all(void) {
    if (!_audio_inited) return;
    SDL_LockAudioDevice(_audio_dev);
    for (int v = 0; v < _DNH_VOICE_MAX; v++) _voices[v].sound_id = 0;
    SDL_UnlockAudioDevice(_audio_dev);
}

void master_volume(double vol) {
    if (vol < 0) vol = 0; if (vol > 1) vol = 1;
    _master_vol = (float)vol;
}

int audio_active_voices(void) {
    if (!_audio_inited) return 0;
    int n = 0;
    SDL_LockAudioDevice(_audio_dev);
    for (int v = 0; v < _DNH_VOICE_MAX; v++) if (_voices[v].sound_id != 0) n++;
    SDL_UnlockAudioDevice(_audio_dev);
    return n;
}
