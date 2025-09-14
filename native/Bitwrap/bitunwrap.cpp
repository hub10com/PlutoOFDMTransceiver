#include "Include/bitunwrap.hpp"

#include <fstream>
#include <vector>
#include <deque>
#include <cstring>
#include <stdexcept>
#include <algorithm>

static std::uint64_t g_last_start_flag_pos = 0;
static std::uint64_t g_last_end_flag_pos   = 0;

BITUNWRAP_API std::uint64_t get_last_start_flag_pos() { return g_last_start_flag_pos; }
BITUNWRAP_API std::uint64_t get_last_end_flag_pos()   { return g_last_end_flag_pos; }

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
    std::ofstream& out_;
    uint8_t acc_;
    uint8_t bit_off_;
};

class BitReader {
public:
    explicit BitReader(std::ifstream& in, size_t buf_bytes)
        : in_(in), buf_(buf_bytes), pos_(0), filled_(0), bit_idx_(8) {}

    int next_bit() {
        if (bit_idx_ >= 8) {
            if (pos_ >= filled_) {
                if (!refill_()) return -1;
            }
            cur_byte_ = static_cast<uint8_t>(buf_[pos_++]);
            bit_idx_ = 0;
        }
        int b = (cur_byte_ >> (7 - bit_idx_)) & 1;
        ++bit_idx_;
        return b;
    }

private:
    bool refill_() {
        in_.read(reinterpret_cast<char*>(buf_.data()), static_cast<std::streamsize>(buf_.size()));
        filled_ = static_cast<size_t>(in_.gcount());
        pos_ = 0;
        return filled_ > 0;
    }

    std::ifstream& in_;
    std::vector<uint8_t> buf_;
    size_t pos_;
    size_t filled_;
    uint8_t cur_byte_{0};
    uint8_t bit_idx_;
};

class BitKMP {
public:
    BitKMP() = default;
    explicit BitKMP(const std::vector<uint8_t>& pat) { reset(pat); }

    void reset(const std::vector<uint8_t>& pat) {
        pat_ = pat;
        const size_t n = pat_.size();
        lps_.assign(n, 0);
        for (size_t i = 1, len = 0; i < n; ) {
            if (pat_[i] == pat_[len]) {
                lps_[i++] = static_cast<int>(++len);
            } else if (len != 0) {
                len = static_cast<size_t>(lps_[len - 1]);
            } else {
                lps_[i++] = 0;
            }
        }
        j_ = 0;
    }

    inline bool feed(uint8_t b) {
        while (j_ > 0 && b != pat_[j_]) j_ = static_cast<size_t>(lps_[j_ - 1]);
        if (b == pat_[j_]) {
            if (++j_ == pat_.size()) { j_ = static_cast<size_t>(lps_[j_ - 1]); return true; }
        }
        return false;
    }

    size_t need() const { return pat_.size(); }

private:
    std::vector<uint8_t> pat_;
    std::vector<int> lps_;
    size_t j_{0};
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

BITUNWRAP_API int unwrap_file_bits(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,
    const char* end_flag_bits)
{
    g_last_start_flag_pos = 0;
    g_last_end_flag_pos   = 0;

    try {
        std::ifstream fin(in_path, std::ios::binary);
        if (!fin) return -1;
        std::ofstream fout(out_path, std::ios::binary);
        if (!fout) return -2;

        constexpr size_t IO_BUF = 8u << 20; 
        std::vector<char> inbuf(IO_BUF), outbuf(IO_BUF);
        fin.rdbuf()->pubsetbuf(inbuf.data(), static_cast<std::streamsize>(inbuf.size()));
        fout.rdbuf()->pubsetbuf(outbuf.data(), static_cast<std::streamsize>(outbuf.size()));

        std::vector<uint8_t> start_bits, end_bits;
        try {
            start_bits = parse_bitstring_(start_flag_bits);
            end_bits   = parse_bitstring_(end_flag_bits);
        } catch (...) {
            return -3;
        }
        if (start_bits.empty() || end_bits.empty()) return -3;

        BitKMP kmp_start(start_bits);
        BitKMP kmp_end(end_bits);

        BitReader br(fin, IO_BUF);
        BitWriter bw(fout);

        std::uint64_t bit_index = 0;

        bool found_start = false;
        while (true) {
            int bit = br.next_bit();
            if (bit < 0) break; 
            bit_index++;        
            if (kmp_start.feed(static_cast<uint8_t>(bit))) {
                g_last_start_flag_pos = bit_index - static_cast<std::uint64_t>(start_bits.size());
                found_start = true;
                break;
            }
        }
        if (!found_start) return -4;

        const size_t Lend = kmp_end.need();
        std::deque<uint8_t> tail; tail.clear();

        bool found_end = false;
        while (true) {
            int bit = br.next_bit();
            if (bit < 0) break; 
            bit_index++;
            uint8_t b = static_cast<uint8_t>(bit);

            tail.push_back(b);
            if (kmp_end.feed(b)) {

                g_last_end_flag_pos = bit_index - static_cast<std::uint64_t>(end_bits.size());

                if (tail.size() >= Lend) {
                    for (size_t i = 0; i < Lend; ++i) tail.pop_back();
                } else {
                    tail.clear(); 
                }
                found_end = true;
                break;
            }

            if (tail.size() > Lend) {
                uint8_t outb = tail.front();
                tail.pop_front();
                bw.write_bit(outb);
            }
        }

        if (!found_end) return -4;

        while (!tail.empty()) {
            bw.write_bit(tail.front());
            tail.pop_front();
        }

        bw.pad_to_byte();

        return 0;
    } catch (...) {
        return -99;
    }
}
