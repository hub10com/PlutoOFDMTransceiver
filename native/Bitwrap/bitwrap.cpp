#include "Include/bitwrap.hpp"

#include <fstream>
#include <vector>
#include <random>
#include <cstring>
#include <stdexcept>
#include <algorithm>

class BitWriter {
public:
    explicit BitWriter(std::ofstream& out) : out_(out), acc_(0), bit_off_(0) {}

    inline void write_bit(uint8_t b) {
        acc_ |= static_cast<uint8_t>((b & 1u) << (7 - bit_off_));
        if (++bit_off_ == 8) flush_byte_();
    }

    inline void write_bits(const uint8_t* bits, size_t n) {
        for (size_t i = 0; i < n; ++i) write_bit(bits[i]);
    }

    inline void write_full_bytes(const uint8_t* bytes, size_t n) {
        if (bit_off_ == 0) {
            out_.write(reinterpret_cast<const char*>(bytes),
                       static_cast<std::streamsize>(n));
        } else {
            write_bytes_as_bits_(bytes, n);
        }
    }

    inline void pad_to_byte() {
        if (bit_off_ == 0) return;
        while (bit_off_ != 0) write_bit(0);
    }

private:
    inline void flush_byte_() {
        out_.put(static_cast<char>(acc_));
        acc_ = 0;
        bit_off_ = 0;
    }

    inline void write_bytes_as_bits_(const uint8_t* bytes, size_t n) {
        for (size_t i = 0; i < n; ++i) {
            uint8_t v = bytes[i];
            for (int b = 7; b >= 0; --b) write_bit((v >> b) & 1u);
        }
    }

    std::ofstream& out_;
    uint8_t acc_;
    uint8_t bit_off_;
};

static std::vector<uint8_t> parse_bitstring_(const char* s) {
    std::vector<uint8_t> v;
    if (!s) return v; 
    const size_t len = std::strlen(s);
    v.reserve(len);
    for (size_t i = 0; i < len; ++i) {
        const char c = s[i];
        if (c == '0') v.push_back(0);
        else if (c == '1') v.push_back(1);
        else throw std::runtime_error("bitstring contains non 0/1 char");
    }
    return v;
}

static void write_dummy_bits_(BitWriter& bw, std::uint64_t nbits, std::mt19937_64& rng) {
    if (nbits == 0) return;

    const std::uint64_t full_bytes = nbits / 8;
    const uint32_t tail_bits = static_cast<uint32_t>(nbits % 8);

    constexpr size_t BUF = 1u << 20; 
    std::vector<uint8_t> tmp(BUF);

    std::uint64_t remaining = full_bytes;
    while (remaining > 0) {
        const size_t chunk = static_cast<size_t>(std::min<std::uint64_t>(remaining, BUF));

        for (size_t i = 0; i < chunk; ++i) tmp[i] = static_cast<uint8_t>(rng() & 0xFFu);
        bw.write_full_bytes(tmp.data(), chunk);
        remaining -= chunk;
    }

    if (tail_bits) {
        const uint8_t last = static_cast<uint8_t>(rng() & 0xFFu);
        for (int b = 7; b >= 8 - static_cast<int>(tail_bits); --b) {
            bw.write_bit((last >> b) & 1u);
        }
    }
}

BITWRAP_API int wrap_file_bits(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,
    const char* end_flag_bits,
    std::uint64_t dummy_left_bits,
    std::uint64_t dummy_right_bits,
    std::uint32_t rng_seed)
{
    try {
        std::ifstream fin(in_path, std::ios::binary);
        if (!fin) return -1;
        std::ofstream fout(out_path, std::ios::binary);
        if (!fout) return -2;

        constexpr size_t IO_BUF = 8u << 20; 
        std::vector<char> inbuf(IO_BUF), outbuf(IO_BUF);
        fin.rdbuf()->pubsetbuf(inbuf.data(), static_cast<std::streamsize>(inbuf.size()));
        fout.rdbuf()->pubsetbuf(outbuf.data(), static_cast<std::streamsize>(outbuf.size()));

        std::mt19937_64 rng;
        if (rng_seed == 0) {
            std::random_device rd;
            std::seed_seq seq{ rd(), rd(), rd(), rd() };
            rng.seed(seq);
        } else {
            rng.seed(rng_seed);
        }

        std::vector<uint8_t> start_bits, end_bits;
        try {
            start_bits = parse_bitstring_(start_flag_bits);
            end_bits   = parse_bitstring_(end_flag_bits);
        } catch (...) {
            return -3;
        }

        BitWriter bw(fout);

        write_dummy_bits_(bw, dummy_left_bits, rng);
        if (!start_bits.empty()) bw.write_bits(start_bits.data(), start_bits.size());

        std::vector<uint8_t> chunk(IO_BUF);
        while (true) {
            fin.read(reinterpret_cast<char*>(chunk.data()),
                     static_cast<std::streamsize>(chunk.size()));
            const std::streamsize got = fin.gcount();
            if (got <= 0) break;
            bw.write_full_bytes(chunk.data(), static_cast<size_t>(got));
        }

        if (!end_bits.empty()) bw.write_bits(end_bits.data(), end_bits.size());
        write_dummy_bits_(bw, dummy_right_bits, rng);

        bw.pad_to_byte();

        return 0;
    } catch (...) {
        return -99;
    }
}

BITWRAP_API int wrap_file_bits_ratio(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,
    const char* end_flag_bits,
    double ratio_divisor,
    std::uint32_t rng_seed)
{
    try {
        if (ratio_divisor <= 0.0) return -4;

        std::ifstream fin(in_path, std::ios::binary | std::ios::ate);
        if (!fin) return -1;
        const std::streamsize file_size_bytes = fin.tellg();
        fin.close();

        if (file_size_bytes <= 0) return -4;

        const std::uint64_t n_bits = static_cast<std::uint64_t>(file_size_bytes) * 8ULL;
        const double each = static_cast<double>(n_bits) / (2.0 * ratio_divisor);
        const std::uint64_t dummy_each = static_cast<std::uint64_t>(each);

        return wrap_file_bits(
            in_path,
            out_path,
            start_flag_bits,
            end_flag_bits,
            dummy_each,
            dummy_each,
            rng_seed
        );
    } catch (...) {
        return -99;
    }
}
