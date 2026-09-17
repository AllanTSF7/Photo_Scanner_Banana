#include <cmath>
#include <cstdlib>
#include <stdexcept>

#include "core.hpp"

namespace banana {

BlankMetrics blank_metrics(const std::uint8_t* pixels, std::size_t height, std::size_t width, int edge_threshold) {
    if (height < 2 || width < 2) {
        throw std::invalid_argument("image too small");
    }

    std::uint64_t total = 0;
    std::uint64_t total_sq = 0;
    std::uint64_t edges = 0;
    for (std::size_t y = 0; y < height; ++y) {
        const std::uint8_t* row = pixels + y * width;
        const std::uint8_t* next = y + 1 < height ? row + width : nullptr;
        for (std::size_t x = 0; x < width; ++x) {
            const int v = row[x];
            total += static_cast<std::uint64_t>(v);
            total_sq += static_cast<std::uint64_t>(v * v);
            if (next != nullptr && x + 1 < width) {
                const int grad = std::abs(row[x + 1] - v) + std::abs(next[x] - v);
                edges += grad > edge_threshold;
            }
        }
    }

    const double n = static_cast<double>(height * width);
    const double mean = static_cast<double>(total) / n;
    const double variance = static_cast<double>(total_sq) / n - mean * mean;
    const double interior = static_cast<double>((height - 1) * (width - 1));
    return {mean, std::sqrt(variance > 0.0 ? variance : 0.0), static_cast<double>(edges) / interior};
}

}  // namespace banana
