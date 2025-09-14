#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>
#include <array>

namespace jd {

class Counter {
public:
    using clock = std::chrono::steady_clock;

    Counter() { _pattern = {1,3,5,4,2}; }

    void start(uint64_t seq) {
        _seq.store(seq, std::memory_order_relaxed);
        _t0 = clock::now();
        _active.store(true, std::memory_order_release);
    }

    void stop() {
        _active.store(false, std::memory_order_release);
    }

    bool active() const { return _active.load(std::memory_order_acquire); }
    uint64_t seq()  const { return _seq.load(std::memory_order_relaxed); }

    // Aktif değilse false döner
    bool current_value(int& out_val) const {
        if (!active()) return false;
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(clock::now() - _t0).count();
        size_t idx = static_cast<size_t>(elapsed % _pattern.size());
        out_val = _pattern[idx];
        return true;
    }

private:
    std::array<int,5> _pattern;
    std::atomic<bool> _active{false};
    std::atomic<uint64_t> _seq{0};
    clock::time_point _t0{};
};

} // namespace jd
