// rs_container.c  (v4 with IL/SLICE EX + Residual BER + Progress/Cancel)
// Reed-Solomon per-column parity container for offline/lossy channels.
// - Keeps on-disk format stable (v4)
// - Adds: pack_ex (IL/Slice), unpack_ex (PAD), residual BER estimate (CRC-based)
// - Progress/cancel callbacks
//
// Build (Linux/macOS):  gcc -O3 -shared -fPIC -o rs_container.so rs_container.c fec.o
// Build (Windows):      cl /O2 /LD rs_container.c fec.obj
//
// Author: (you)
// Date: 2025-08-13

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "fec.h"

#ifdef _WIN32
#define DLL_EXPORT __declspec(dllexport)
#else
#define DLL_EXPORT
#endif

// -------------------- Padding policy --------------------
// 0 = RAW (leave as-is), 1 = ZERO (column to 0), 2 = TEMPORAL (copy previous frame’s column)
#ifndef RS_PAD_MODE
#define RS_PAD_MODE 0
#endif

// -------------------- Constants --------------------
#define K_SHARDS         192                 // data shards
#define SHARD_LEN        64                  // bytes per shard
#define FRAME_BYTES      (K_SHARDS * SHARD_LEN)  // 12,288 bytes
#define MAX_R            63                  // 192 + r <= 255

// v4 container magics (LE)
#define GLOBAL_MAGIC     0x54435352u         // 'RSCT'
#define FRAME_MAGIC_V4   0x34534652u         // 'RSF4'
#define SLICE_MAGIC_V4   0x344C5352u         // 'RSL4'

// Defaults (interleaving)
#ifndef IL_DEPTH_DEFAULT
#define IL_DEPTH_DEFAULT 16                  // frames per interleave group
#endif
#ifndef SLICE_BYTES_DEFAULT
#define SLICE_BYTES_DEFAULT 512              // slice size
#endif

// Optional log
#ifndef RS_LOG
#define RS_LOG 0
#endif
#if RS_LOG
#define LOGF(...) fprintf(stderr, __VA_ARGS__)
#else
#define LOGF(...) do{}while(0)
#endif

// -------------------- Residual BER coefficient --------------------
// Bozuk shard tespit edildiğinde (decode SONRASI CRC mismatch), o shard içinde
// beklenen kötü bayt sayısını SHARD_LEN * coeff olarak varsayıyoruz.
#ifndef RS_RESIDUAL_COEFF_DEFAULT
#define RS_RESIDUAL_COEFF_DEFAULT 0.40
#endif
static double g_residual_coeff = RS_RESIDUAL_COEFF_DEFAULT;

// API: GUI’den ayarlamak için
DLL_EXPORT void rs_set_residual_coeff(double v) {
    if (v < 0.0) v = 0.0;
    if (v > 1.0) v = 1.0;
    g_residual_coeff = v;
}

// -------------------- Progress/Cancel API --------------------
typedef void (*rs_progress_cb)(uint64_t done, uint64_t total); // slice count (packing)/estimated slices (unpack)
static rs_progress_cb g_cb = NULL;
static volatile int g_cancel = 0;

DLL_EXPORT void rs_set_progress_cb(rs_progress_cb cb) { g_cb = cb; }
DLL_EXPORT void rs_request_cancel(int yes) { g_cancel = yes ? 1 : 0; }

// -------------------- Headers ----------------------------
#pragma pack(push, 1)
typedef struct {
    uint32_t magic;         // 'RSCT'
    uint16_t version;       // 4
    uint16_t k;             // 192
    uint16_t r;             // e.g., 16
    uint16_t shard_len;     // 64
    uint16_t pad;           // 255 - (k+r)
    uint64_t original_size; // bytes
    uint64_t frame_count;   // total frames
    uint16_t il_depth;      // interleave depth (e.g., 16)
    uint16_t slice_bytes;   // slice size (e.g., 512)
    uint16_t reserved;      // alignment
} rsct_header_v4_t;

typedef struct {
    uint32_t magic;         // 'RSF4'
    uint64_t index;         // frame index (0..)
    uint16_t data_len;      // valid data bytes in this frame (<= 12288)
    uint16_t parity_len;    // parity bytes (= r*64)
    uint32_t crc32_data;    // CRC32 of data block (12288B)
    uint32_t crc32_par;     // CRC32 of parity block (r*64B)
} frame_hdr_v4_t;

typedef struct {
    uint32_t magic;         // 'RSL4'
    uint64_t frame_index;   // which frame this slice belongs to
    uint32_t offset;        // offset within frame payload
    uint16_t size;          // slice byte count
    uint32_t crc32_slice;   // CRC32 of slice data
} slice_hdr_v4_t;
#pragma pack(pop)

// -------------------- CRC ----------------------------
static uint32_t crc32_table[256];
static int crc32_init_done = 0;
static void crc32_init(void){
    if (crc32_init_done) return;
    for (uint32_t i=0;i<256;i++){
        uint32_t c=i;
        for (int j=0;j<8;j++)
            c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc32_table[i]=c;
    }
    crc32_init_done = 1;
}
static uint32_t crc32_calc(const uint8_t* buf, size_t len){
    crc32_init();
    uint32_t c=0xFFFFFFFFu;
    for (size_t i=0;i<len;i++)
        c = crc32_table[(c ^ buf[i]) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

static uint16_t crc16_table[256];
static int crc16_init_done = 0;
static void crc16_init(void){
    if (crc16_init_done) return;
    for (int i=0;i<256;i++){
        uint16_t c = (uint16_t)(i << 8);
        for (int j=0;j<8;j++)
            c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
        crc16_table[i] = c;
    }
    crc16_init_done = 1;
}
static uint16_t crc16_ccitt(const uint8_t *buf, size_t len){
    crc16_init();
    uint16_t crc = 0xFFFF;
    for (size_t i=0;i<len;i++)
        crc = (uint16_t)((crc << 8) ^ crc16_table[((crc >> 8) ^ buf[i]) & 0xFF]);
    return crc;
}

// -------------------- Utils --------------------
static int64_t ftell64_(FILE* f){
#ifdef _WIN32
    return _ftelli64(f);
#else
    return ftell(f);
#endif
}
static int fseek64_(FILE* f, int64_t off, int wh){
#ifdef _WIN32
    return _fseeki64(f, off, wh);
#else
    return fseek(f, off, wh);
#endif
}
static int get_file_size64(FILE *f, uint64_t *out) {
    int64_t cur = ftell64_(f);
    if (cur < 0) return -1;
    if (fseek64_(f, 0, SEEK_END) != 0) return -1;
    int64_t end = ftell64_(f);
    if (end < 0) { fseek64_(f, cur, SEEK_SET); return -1; }
    *out = (uint64_t)end;
    if (fseek64_(f, cur, SEEK_SET) != 0) return -1;
    return 0;
}
static int compute_pad(int r) { return 255 - (K_SHARDS + r); }
static size_t payload_len_bytes(int r){
    return (size_t)FRAME_BYTES + (size_t)r * SHARD_LEN
         + (size_t)K_SHARDS * 2u + (size_t)r * 2u;
}

// -------------------- Stats (decode sonrası) --------------------
// Not: SER’e ihtiyacın yok dedin; alan dursa da doldurmuyoruz (0).
typedef struct {
    uint64_t frames_total;
    uint64_t slices_total_est;
    uint64_t slices_ok;
    uint64_t slices_bad;
    uint64_t codewords_total;     // SHARD_LEN * frame_count
    uint64_t symbols_total;       // (K+r) * codewords_total
    uint64_t data_symbols_total;  // K * codewords_total
    uint64_t corrected_symbols;   // info only (not used for SER)
    uint64_t used_erasures_cols;  // info
    uint64_t rs_fail_columns;     // info
    int      pad_mode_used;
    double   ser_rs;              // kept for ABI; stays 0.0
    double   ber_est;             // residual BER estimate
} rs_stats_v1_t;

static rs_stats_v1_t g_rs_stats;
static void rs_stats_reset(void){
    memset(&g_rs_stats, 0, sizeof(g_rs_stats));
}
DLL_EXPORT void rs_get_stats_v1(rs_stats_v1_t* out) {
    if (!out) return;
    *out = g_rs_stats;
}

// -------------------- RS encode (column-wise) --------------------
static int encode_frame_parity(void *rs, const uint8_t *frame, size_t valid_len,
                               int r, uint8_t *par_out /*r*64*/)
{
    uint8_t cw[K_SHARDS + MAX_R];
    for (int i = 0; i < SHARD_LEN; ++i) {
        for (int j = 0; j < K_SHARDS; ++j) {
            size_t idx = (size_t)j * SHARD_LEN + (size_t)i;
            cw[j] = (idx < valid_len) ? frame[idx] : 0;
        }
        encode_rs_char(rs, cw, &cw[K_SHARDS]);
        for (int j = 0; j < r; ++j)
            par_out[j * SHARD_LEN + i] = cw[K_SHARDS + j];
    }
    return 0;
}

// -------------------- Resync helper --------------------
static int find_next_magic(FILE* f, uint32_t *out_magic){
    uint8_t win[4];
    size_t g = fread(win,1,4,f);
    if (g < 4) return 0;
    for(;;){
        uint32_t v = (uint32_t)win[0] | ((uint32_t)win[1]<<8) | ((uint32_t)win[2]<<16) | ((uint32_t)win[3]<<24);
        if (v == FRAME_MAGIC_V4 || v == SLICE_MAGIC_V4) { *out_magic = v; return 1; }
        int c = fgetc(f);
        if (c == EOF) return 0;
        win[0]=win[1]; win[1]=win[2]; win[2]=win[3]; win[3]=(uint8_t)c;
    }
}

// -------------------- Frame buffer (decode) --------------------
typedef struct {
    int          init;        // 0: none, 1: header seen, 2: placeholder (slice seen)
    uint16_t     data_len;    // real data bytes for last frame (<= FRAME_BYTES)
    uint8_t     *data;        // 12288
    uint8_t     *par;         // r*64
    uint16_t    *crcD;        // 192 entries
    uint16_t    *crcP;        // r entries
    uint32_t     crc32_data;
    uint32_t     crc32_par;
    size_t       crcD_filled_bytes;
    size_t       crcP_filled_bytes;
} frame_buf_t;

static void copy_slice_into_frame(frame_buf_t *fb, int r, uint32_t off, const uint8_t *src, uint16_t len,
                                  size_t *o_data, size_t *o_par, size_t *o_crcD, size_t *o_crcP)
{
    const size_t par_bytes  = (size_t)r * SHARD_LEN;
    const size_t crcD_bytes = (size_t)K_SHARDS * 2u;
    const size_t crcP_bytes = (size_t)r * 2u;

    size_t copied = 0, c_data=0, c_par=0, c_crcD=0, c_crcP=0;

    if (off < FRAME_BYTES) {
        size_t m = FRAME_BYTES - off;
        size_t take = (len < m) ? len : m;
        memcpy(fb->data + off, src, take);
        copied += take; c_data += take;
    }
    if (off + copied < FRAME_BYTES + par_bytes && copied < len) {
        size_t base = FRAME_BYTES;
        if (off + copied >= base) {
            size_t soff = (off + copied - base);
            size_t m = par_bytes - soff;
            size_t take = ((len - copied) < m) ? (len - copied) : m;
            memcpy(fb->par + soff, src + copied, take);
            copied += take; c_par += take;
        }
    }
    if (off + copied < FRAME_BYTES + par_bytes + crcD_bytes && copied < len) {
        size_t base = FRAME_BYTES + par_bytes;
        if (off + copied >= base) {
            size_t soff = (off + copied - base);
            size_t m = crcD_bytes - soff;
            size_t take = ((len - copied) < m) ? (len - copied) : m;
            memcpy(((uint8_t*)fb->crcD) + soff, src + copied, take);
            copied += take; c_crcD += take;
        }
    }
    if (copied < len) {
        size_t base = FRAME_BYTES + par_bytes + crcD_bytes;
        if (off + copied >= base) {
            size_t soff = (off + copied - base);
            size_t m = crcP_bytes - soff;
            size_t take = ((len - copied) < m) ? (len - copied) : m;
            memcpy(((uint8_t*)fb->crcP) + soff, src + copied, take);
            copied += take; c_crcP += take;
        }
    }

    fb->crcD_filled_bytes += c_crcD;
    fb->crcP_filled_bytes += c_crcP;

    if (o_data) *o_data = c_data;
    if (o_par)  *o_par  = c_par;
    if (o_crcD) *o_crcD = c_crcD;
    if (o_crcP) *o_crcP = c_crcP;
}

// -------------------- Encoder (pack) --------------------
static int pack_impl(const char *input_path, const char *container_path, int r,
                     int il_depth, int slice_bytes)
{
    if (r <= 0 || r > MAX_R) r = 16;
    if (il_depth <= 0) il_depth = IL_DEPTH_DEFAULT;
    if (slice_bytes <= 0) slice_bytes = SLICE_BYTES_DEFAULT;

    int pad = compute_pad(r);
    if (pad < 0) return -101;

    void *rs = init_rs_char(8, 0x11d, 1, 1, r, pad);
    if (!rs) return -1;

    FILE *fi = fopen(input_path, "rb");
    if (!fi) { return -2; }
    FILE *fo = fopen(container_path, "wb");
    if (!fo) { fclose(fi); return -3; }

    setvbuf(fi, NULL, _IOFBF, 1<<20);
    setvbuf(fo, NULL, _IOFBF, 1<<20);

    uint64_t orig = 0;
    if (get_file_size64(fi, &orig) != 0) { fclose(fi); fclose(fo); return -4; }
    uint64_t frames = (orig + FRAME_BYTES - 1) / FRAME_BYTES;

    rsct_header_v4_t gh = {0};
    gh.magic = GLOBAL_MAGIC;
    gh.version = 4;
    gh.k = K_SHARDS;
    gh.r = (uint16_t)r;
    gh.shard_len = SHARD_LEN;
    gh.pad = (uint16_t)pad;
    gh.original_size = orig;
    gh.frame_count = frames;
    gh.il_depth = (uint16_t)il_depth;
    gh.slice_bytes = (uint16_t)slice_bytes;
    gh.reserved = 0;

    if (fwrite(&gh, sizeof(gh), 1, fo) != 1) { fclose(fi); fclose(fo); return -5; }

    const uint16_t D = gh.il_depth;
    const uint16_t S = gh.slice_bytes;
    const size_t   PAY = payload_len_bytes(r);
    const uint64_t total_slices = frames * ((PAY + S - 1)/S);
    uint64_t prog_slices = 0;

    uint8_t  **buf_data = (uint8_t**) calloc(D, sizeof(uint8_t*));
    uint8_t  **buf_par  = (uint8_t**) calloc(D, sizeof(uint8_t*));
    uint16_t **tab_crcD = (uint16_t**)calloc(D, sizeof(uint16_t*));
    uint16_t **tab_crcP = (uint16_t**)calloc(D, sizeof(uint16_t*));
    frame_hdr_v4_t *fhdr = (frame_hdr_v4_t*)calloc(D, sizeof(frame_hdr_v4_t));
    if (!buf_data || !buf_par || !tab_crcD || !tab_crcP || !fhdr) {
        free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
        fclose(fi); fclose(fo); return -6;
    }

    uint64_t fbase = 0;
    while (fbase < frames) {
        if (g_cancel) { LOGF("[pack] cancel\n"); break; }

        uint16_t in_grp = (uint16_t)((frames - fbase) >= D ? D : (frames - fbase));

        for (uint16_t gi = 0; gi < in_grp; ++gi) {
            uint64_t fidx = fbase + gi;

            buf_data[gi] = (uint8_t*) malloc(FRAME_BYTES);
            buf_par[gi]  = (uint8_t*) malloc((size_t)r * SHARD_LEN);
            tab_crcD[gi] = (uint16_t*)malloc(sizeof(uint16_t) * K_SHARDS);
            tab_crcP[gi] = (uint16_t*)malloc(sizeof(uint16_t) * r);
            if (!buf_data[gi] || !buf_par[gi] || !tab_crcD[gi] || !tab_crcP[gi]) {
                for (uint16_t k=0;k<in_grp;k++){
                    free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                }
                free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                fclose(fi); fclose(fo);
                return -7;
            }

            size_t to_read = FRAME_BYTES;
            if (fidx == frames - 1) {
                uint64_t remain = orig - fidx * (uint64_t)FRAME_BYTES;
                if (remain < FRAME_BYTES) to_read = (size_t)remain;
            }
            size_t got = fread(buf_data[gi], 1, to_read, fi);
            if (got < to_read) memset(buf_data[gi] + got, 0, FRAME_BYTES - got);
            if (to_read < FRAME_BYTES) memset(buf_data[gi] + to_read, 0, FRAME_BYTES - to_read);

            if (encode_frame_parity(rs, buf_data[gi], to_read, r, buf_par[gi]) != 0) {
                for (uint16_t k=0;k<in_grp;k++){
                    free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                }
                free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                fclose(fi); fclose(fo);
                return -8;
            }

            for (int j=0;j<K_SHARDS;j++)
                tab_crcD[gi][j] = crc16_ccitt(buf_data[gi] + (size_t)j*SHARD_LEN, SHARD_LEN);
            for (int j=0;j<r;j++)
                tab_crcP[gi][j] = crc16_ccitt(buf_par[gi]  + (size_t)j*SHARD_LEN, SHARD_LEN);

            frame_hdr_v4_t fh;
            fh.magic      = FRAME_MAGIC_V4;
            fh.index      = fidx;
            fh.data_len   = (uint16_t)to_read;
            fh.parity_len = (uint16_t)(r * SHARD_LEN);
            fh.crc32_data = crc32_calc(buf_data[gi], FRAME_BYTES);
            fh.crc32_par  = crc32_calc(buf_par[gi],   (size_t)r * SHARD_LEN);
            fhdr[gi] = fh;

            if (fwrite(&fh, sizeof(fh), 1, fo) != 1) {
                for (uint16_t k=0;k<in_grp;k++){
                    free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                }
                free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                fclose(fi); fclose(fo);
                return -9;
            }
        }

        const size_t par_bytes = (size_t)r * SHARD_LEN;
        const size_t crcD_bytes = (size_t)K_SHARDS * 2u;
        const size_t crcP_bytes = (size_t)r * 2u;

        for (size_t off = 0; off < PAY; off += S) {
            if (g_cancel) { LOGF("[pack] cancel\n"); break; }

            size_t chunk = (off + S <= PAY) ? S : (PAY - off);

            for (uint16_t gi = 0; gi < in_grp; ++gi) {
                slice_hdr_v4_t sh;
                sh.magic = SLICE_MAGIC_V4;
                sh.frame_index = fhdr[gi].index;
                sh.offset = (uint32_t)off;
                sh.size   = (uint16_t)chunk;

                uint8_t *ptmp = (uint8_t*)malloc(chunk);
                if (!ptmp) {
                    for (uint16_t k=0;k<in_grp;k++){
                        free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                    }
                    free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                    fclose(fi); fclose(fo);
                    return -10;
                }
                size_t copied = 0;

                if (off < FRAME_BYTES) {
                    size_t m = FRAME_BYTES - off;
                    size_t take = (chunk < m) ? chunk : m;
                    memcpy(ptmp + copied, buf_data[gi] + off, take);
                    copied += take;
                }
                if (off + copied < FRAME_BYTES + par_bytes && copied < chunk) {
                    size_t base = FRAME_BYTES;
                    if (off + copied >= base) {
                        size_t soff = (off + copied - base);
                        size_t m = par_bytes - soff;
                        size_t take = ((chunk - copied) < m) ? (chunk - copied) : m;
                        memcpy(ptmp + copied, buf_par[gi] + soff, take);
                        copied += take;
                    }
                }
                if (off + copied < FRAME_BYTES + par_bytes + crcD_bytes && copied < chunk) {
                    size_t base = FRAME_BYTES + par_bytes;
                    if (off + copied >= base) {
                        size_t soff = (off + copied - base);
                        size_t m = crcD_bytes - soff;
                        size_t take = ((chunk - copied) < m) ? (chunk - copied) : m;
                        memcpy(ptmp + copied, ((uint8_t*)tab_crcD[gi]) + soff, take);
                        copied += take;
                    }
                }
                if (copied < chunk) {
                    size_t base = FRAME_BYTES + par_bytes + crcD_bytes;
                    if (off + copied >= base) {
                        size_t soff = (off + copied - base);
                        size_t m = crcP_bytes - soff;
                        size_t take = ((chunk - copied) < m) ? (chunk - copied) : m;
                        memcpy(ptmp + copied, ((uint8_t*)tab_crcP[gi]) + soff, take);
                        copied += take;
                    }
                }

                sh.crc32_slice = crc32_calc(ptmp, chunk);
                if (fwrite(&sh, sizeof(sh), 1, fo) != 1) { free(ptmp);
                    for (uint16_t k=0;k<in_grp;k++){
                        free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                    }
                    free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                    fclose(fi); fclose(fo);
                    return -11;
                }
                if (fwrite(ptmp, 1, chunk, fo) != chunk) { free(ptmp);
                    for (uint16_t k=0;k<in_grp;k++){
                        free(buf_data[k]); free(buf_par[k]); free(tab_crcD[k]); free(tab_crcP[k]);
                    }
                    free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);
                    fclose(fi); fclose(fo);
                    return -12;
                }
                free(ptmp);

                if (g_cb) g_cb(++prog_slices, total_slices);
            }
        }

        for (uint16_t gi=0; gi<in_grp; ++gi){
            free(buf_data[gi]); free(buf_par[gi]);
            free(tab_crcD[gi]); free(tab_crcP[gi]);
            buf_data[gi]=NULL; buf_par[gi]=NULL; tab_crcD[gi]=NULL; tab_crcP[gi]=NULL;
        }
        fbase += in_grp;
    }

    free(buf_data); free(buf_par); free(tab_crcD); free(tab_crcP); free(fhdr);

#if defined(free_rs_char)
    free_rs_char(rs);
#else
    (void)rs;
#endif

    fclose(fi); fclose(fo);
    return g_cancel ? 1 : 0;
}

DLL_EXPORT
int rs_pack_container(const char *input_path, const char *container_path, int r) {
    return pack_impl(input_path, container_path, r, IL_DEPTH_DEFAULT, SLICE_BYTES_DEFAULT);
}
DLL_EXPORT
int rs_pack_container_ex(const char *input_path, const char *container_path, int r,
                         int il_depth, int slice_bytes)
{
    return pack_impl(input_path, container_path, r, il_depth, slice_bytes);
}

// -------------------- Decoder (v4) --------------------
typedef struct {
    int pad_mode;  // 0 RAW, 1 ZERO, 2 TEMPORAL
} rs_unpack_opts_t;

static int rs_unpack_internal(const char *container_path, const char *output_path, const rs_unpack_opts_t *opts) {
    const int pad_mode = opts ? opts->pad_mode : RS_PAD_MODE;

    rs_stats_reset();

    FILE *fi = fopen(container_path, "rb");
    if (!fi) return -1;
    FILE *fo = fopen(output_path, "wb");
    if (!fo) { fclose(fi); return -7; }

    setvbuf(fi, NULL, _IOFBF, 1<<20);
    setvbuf(fo, NULL, _IOFBF, 1<<20);

    rsct_header_v4_t gh;
    if (fread(&gh, sizeof(gh), 1, fi) != 1) { fclose(fi); fclose(fo); return -2; }
    if (gh.magic != GLOBAL_MAGIC || gh.version != 4) { fclose(fi); fclose(fo); return -3; }
    if (gh.k != K_SHARDS || gh.shard_len != SHARD_LEN) { fclose(fi); fclose(fo); return -4; }

    int r = (int)gh.r;
    if (r <= 0 || r > MAX_R) { fclose(fi); fclose(fo); return -5; }

    void *rs = init_rs_char(8, 0x11d, 1, 1, r, (int)gh.pad);
    if (!rs) { fclose(fi); fclose(fo); return -6; }

    const size_t par_bytes  = (size_t)r * SHARD_LEN;
    const size_t crcD_bytes = (size_t)K_SHARDS * 2u;
    const size_t crcP_bytes = (size_t)r * 2u;
    const size_t PAY        = payload_len_bytes(r);

    uint64_t F = gh.frame_count;
    frame_buf_t *tab = (frame_buf_t*)calloc((size_t)F, sizeof(frame_buf_t));
    if (!tab) {
#if defined(free_rs_char)
        free_rs_char(rs);
#endif
        fclose(fi); fclose(fo);
        return -8;
    }

    g_rs_stats.frames_total        = F;
    g_rs_stats.pad_mode_used       = pad_mode;
    g_rs_stats.codewords_total     = (uint64_t)SHARD_LEN * (uint64_t)F;
    g_rs_stats.symbols_total       = (uint64_t)(K_SHARDS + r) * g_rs_stats.codewords_total;
    g_rs_stats.data_symbols_total  = (uint64_t)K_SHARDS * g_rs_stats.codewords_total;
    if (gh.slice_bytes)
        g_rs_stats.slices_total_est = F * ((PAY + gh.slice_bytes - 1) / gh.slice_bytes);

    uint64_t residual_bad_bytes_est = 0;
    uint64_t total_written_bytes    = 0;

    uint64_t total_slices = 0, done_slices = 0;
    if (gh.slice_bytes) total_slices = F * ((PAY + gh.slice_bytes - 1) / gh.slice_bytes);

    for (;;) {
        if (g_cancel) { LOGF("[unpack] cancel\n"); break; }

        uint32_t magic=0;
        if (!find_next_magic(fi, &magic)) break;

        if (magic == FRAME_MAGIC_V4) {
            frame_hdr_v4_t fh;
            if (fread(((uint8_t*)&fh)+4, 1, sizeof(fh)-4, fi) != sizeof(fh)-4) break;
            uint64_t idx = fh.index;
            if (idx >= F) continue;

            if (fh.parity_len != (uint16_t)(r*SHARD_LEN)) continue;
            if (fh.data_len > FRAME_BYTES) continue;

            frame_buf_t *fb = &tab[idx];
            if (!fb->init) {
                fb->data  = (uint8_t*)  calloc(1, FRAME_BYTES);
                fb->par   = (uint8_t*)  calloc(1, par_bytes);
                fb->crcD  = (uint16_t*) calloc(K_SHARDS, sizeof(uint16_t));
                fb->crcP  = (uint16_t*) calloc(r, sizeof(uint16_t));
                if (!fb->data || !fb->par || !fb->crcD || !fb->crcP) {
                    if (fb->data) free(fb->data);
                    if (fb->par)  free(fb->par);
                    if (fb->crcD) free(fb->crcD);
                    if (fb->crcP) free(fb->crcP);
                    memset(fb,0,sizeof(*fb));
                    continue;
                }
                fb->init = 1;
            }
            fb->data_len   = fh.data_len;
            fb->crc32_data = fh.crc32_data;
            fb->crc32_par  = fh.crc32_par;
        }
        else if (magic == SLICE_MAGIC_V4) {
            slice_hdr_v4_t sh;
            if (fread(((uint8_t*)&sh)+4, 1, sizeof(sh)-4, fi) != sizeof(sh)-4) break;
            uint16_t size = sh.size;
            if (size == 0) continue;

            uint8_t *buf = (uint8_t*)malloc(size);
            if (!buf) { fseek(fi, size, SEEK_CUR); continue; }
            if (fread(buf, 1, size, fi) != size) { free(buf); break; }

            if (crc32_calc(buf, size) != sh.crc32_slice) {
                g_rs_stats.slices_bad++;
                free(buf);
                continue;
            }
            g_rs_stats.slices_ok++;

            if (sh.frame_index < F) {
                frame_buf_t *fb = &tab[sh.frame_index];
                if (!fb->init) {
                    fb->data  = (uint8_t*)  calloc(1, FRAME_BYTES);
                    fb->par   = (uint8_t*)  calloc(1, par_bytes);
                    fb->crcD  = (uint16_t*) calloc(K_SHARDS, sizeof(uint16_t));
                    fb->crcP  = (uint16_t*) calloc(r, sizeof(uint16_t));
                    if (!fb->data || !fb->par || !fb->crcD || !fb->crcP) {
                        if (fb->data) free(fb->data);
                        if (fb->par)  free(fb->par);
                        if (fb->crcD) free(fb->crcD);
                        if (fb->crcP) free(fb->crcP);
                        memset(fb,0,sizeof(*fb));
                        free(buf);
                        continue;
                    }
                    if (sh.frame_index == F-1) {
                        uint64_t last_bytes = gh.original_size - (F-1) * (uint64_t)FRAME_BYTES;
                        fb->data_len = (uint16_t)((last_bytes <= FRAME_BYTES) ? last_bytes : FRAME_BYTES);
                    } else {
                        fb->data_len = FRAME_BYTES;
                    }
                    fb->init = 2;
                }
                size_t a,b,c,d;
                copy_slice_into_frame(fb, r, sh.offset, buf, sh.size, &a,&b,&c,&d);
            }
            free(buf);

            if (g_cb) g_cb(++done_slices, total_slices);
        }
    }

    uint8_t *code = (uint8_t*)malloc(K_SHARDS + MAX_R);
    if (!code) {
#if defined(free_rs_char)
        free_rs_char(rs);
#endif
        for (uint64_t k=0;k<F;k++){
            if (tab[k].data) free(tab[k].data);
            if (tab[k].par)  free(tab[k].par);
            if (tab[k].crcD) free(tab[k].crcD);
            if (tab[k].crcP) free(tab[k].crcP);
        }
    free_tab_and_files:
        free(tab); fclose(fi); fclose(fo); return -9;
    }

    uint64_t written = 0;
    int erasures[K_SHARDS + MAX_R];

    for (uint64_t idx=0; idx<F; ++idx) {
        if (g_cancel) { LOGF("[unpack] cancel\n"); break; }

        frame_buf_t *fb = &tab[idx];
        if (!fb->init) {
            size_t to_write = (size_t)((gh.original_size - written) >= FRAME_BYTES ? FRAME_BYTES
                                                                                  : (gh.original_size - written));
            if (to_write > 0) {
                uint8_t zbuf[1024]; memset(zbuf,0,sizeof(zbuf));
                size_t left = to_write;
                while (left) {
                    size_t n = (left > sizeof(zbuf)) ? sizeof(zbuf) : left;
                    fwrite(zbuf,1,n,fo); left -= n;
                }
                written += to_write;
                total_written_bytes += to_write;
            }
            continue;
        }

        int eras_data[K_SHARDS]; int nd=0;
        int eras_par[MAX_R];     int np=0;

        size_t dlen = fb->data_len; if (dlen > FRAME_BYTES) dlen = FRAME_BYTES;
        if (dlen < FRAME_BYTES) {
            size_t full2 = dlen / SHARD_LEN, rem2 = dlen % SHARD_LEN;
            size_t cutoff2 = full2 + (rem2 ? 1 : 0);
            for (size_t j = cutoff2; j < K_SHARDS; ++j) eras_data[nd++] = (int)j;
            if (rem2) eras_data[nd++] = (int)full2;
        }

        bool has_crc_tables = (fb->crcD_filled_bytes >= crcD_bytes) && (fb->crcP_filled_bytes >= crcP_bytes);

        if (has_crc_tables) {
            for (int j=0;j<K_SHARDS;j++){
                uint16_t c = crc16_ccitt(fb->data + (size_t)j*SHARD_LEN, SHARD_LEN);
                if (c != fb->crcD[j]) eras_data[nd++] = j;
            }
            for (int j=0;j<r;j++){
                uint16_t c = crc16_ccitt(fb->par + (size_t)j*SHARD_LEN, SHARD_LEN);
                if (c != fb->crcP[j]) eras_par[np++] = (int)(K_SHARDS + j);
            }
        }

        int n_eras = 0;
        for (int i=0; i<nd && n_eras<r; ++i) erasures[n_eras++] = eras_data[i];
        for (int i=0; i<np && n_eras<r; ++i) erasures[n_eras++] = eras_par[i];

        for (int i = 0; i < SHARD_LEN; ++i) {
            for (int j = 0; j < K_SHARDS; ++j) {
                size_t id = (size_t)j * SHARD_LEN + (size_t)i;
                code[j] = fb->data[id];
            }
            for (int j = 0; j < r; ++j)
                code[K_SHARDS + j] = fb->par[j * SHARD_LEN + i];

            int ret = decode_rs_char(rs, code, (n_eras ? erasures : NULL), n_eras);

            if (n_eras > 0) g_rs_stats.used_erasures_cols++;
            if (ret < 0) {
                g_rs_stats.rs_fail_columns++;
                if (pad_mode == 1) {             // ZERO
                    for (int j = 0; j < K_SHARDS; ++j) {
                        size_t id = (size_t)j * SHARD_LEN + (size_t)i;
                        fb->data[id] = 0;
                    }
                } else if (pad_mode == 2) {      // TEMPORAL
                    if (idx > 0 && tab[idx-1].init && tab[idx-1].data) {
                        for (int j = 0; j < K_SHARDS; ++j) {
                            size_t id = (size_t)j * SHARD_LEN + (size_t)i;
                            fb->data[id] = tab[idx-1].data[id];
                        }
                    } else {
                        for (int j = 0; j < K_SHARDS; ++j) {
                            size_t id = (size_t)j * SHARD_LEN + (size_t)i;
                            fb->data[id] = 0;
                        }
                    }
                } else { /* RAW */ }
            } else {
                g_rs_stats.corrected_symbols += (uint64_t)ret;
                for (int j = 0; j < K_SHARDS; ++j) {
                    size_t id = (size_t)j * SHARD_LEN + (size_t)i;
                    fb->data[id] = code[j];
                }
            }
        }

        // Residual error observation (after decode), only if CRC tables present
        if (has_crc_tables) {
            for (int j = 0; j < K_SHARDS; ++j) {
                uint16_t c = crc16_ccitt(fb->data + (size_t)j*SHARD_LEN, SHARD_LEN);
                if (c != fb->crcD[j]) {
                    residual_bad_bytes_est += (uint64_t)((double)SHARD_LEN * g_residual_coeff);
                }
            }
        }

        size_t to_write = (size_t)((gh.original_size - written) >= FRAME_BYTES ? FRAME_BYTES
                                                                              : (gh.original_size - written));
        if (to_write > 0) {
            if (fwrite(fb->data, 1, to_write, fo) != to_write) {
#if defined(free_rs_char)
                free_rs_char(rs);
#endif
                free(code);
                for (uint64_t k=0;k<F;k++){
                    if (tab[k].data) free(tab[k].data);
                    if (tab[k].par)  free(tab[k].par);
                    if (tab[k].crcD) free(tab[k].crcD);
                    if (tab[k].crcP) free(tab[k].crcP);
                }
                free(tab); fclose(fi); fclose(fo); return -10;
            }
            written += to_write;
            total_written_bytes += to_write;
        }
    }

#if defined(free_rs_char)
    free_rs_char(rs);
#endif
    free(code);
    for (uint64_t k=0;k<F;k++){
        if (tab[k].data) free(tab[k].data);
        if (tab[k].par)  free(tab[k].par);
        if (tab[k].crcD) free(tab[k].crcD);
        if (tab[k].crcP) free(tab[k].crcP);
    }
    free(tab);
    fclose(fi); fclose(fo);

    // Final metrics:
    // SER'i istemiyorsun; ser_rs = 0.0 bırakıyoruz.
    // BER: decode sonrası residual gözleme dayalı; CRC gelmediyse 0 kalır (fallback yok).
    if (total_written_bytes > 0 && residual_bad_bytes_est > 0) {
        g_rs_stats.ber_est = (double)residual_bad_bytes_est / (double)total_written_bytes;
    } else {
        g_rs_stats.ber_est = 0.0; // CRC tabloları yoksa ya da tümü düzeldiyse
    }

    return g_cancel ? 1 : 0;
}

DLL_EXPORT
int rs_unpack_container(const char *container_path, const char *output_path) {
    rs_unpack_opts_t opt = { .pad_mode = RS_PAD_MODE };
    return rs_unpack_internal(container_path, output_path, &opt);
}

DLL_EXPORT
int rs_unpack_container_ex(const char *container_path, const char *output_path, int pad_mode) {
    if (pad_mode < 0 || pad_mode > 2) pad_mode = RS_PAD_MODE;
    rs_unpack_opts_t opt = { .pad_mode = pad_mode };
    return rs_unpack_internal(container_path, output_path, &opt);
}
