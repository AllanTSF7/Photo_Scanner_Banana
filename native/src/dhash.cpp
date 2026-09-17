#include <array>
#include <stdexcept>

#include "core.hpp"

namespace banana {
namespace {

constexpr std::size_t kRows = 8;
constexpr std::size_t kCols = 9;

}  // namespace

std::uint64_t dhash(const std::uint8_t* pixels, std::size_t height, std::size_t width) {
    if (height < kRows || width < kCols) {
        throw std::invalid_argument("image too small for dHash");
    }

    // Block of each row/column: bounds are floor(i * size / parts), same as the Python reference.
    std::vector<std::uint8_t> row_block(height), col_block(width);
    std::array<std::uint64_t, kRows> row_count{};
    std::array<std::uint64_t, kCols> col_count{};
    for (std::size_t r = 0, y = 0; r < kRows; ++r) {
        const std::size_t end = (r + 1) * height / kRows;
        for (; y < end; ++y) row_block[y] = static_cast<std::uint8_t>(r);
        row_count[r] = end - r * height / kRows;
    }
    for (std::size_t c = 0, x = 0; c < kCols; ++c) {
        const std::size_t end = (c + 1) * width / kCols;
        for (; x < end; ++x) col_block[x] = static_cast<std::uint8_t>(c);
        col_count[c] = end - c * width / kCols;
    }

    std::array<std::uint64_t, kRows * kCols> sums{};
    std::array<std::uint64_t, kCols> row_sums{};
    for (std::size_t y = 0; y < height; ++y) {
        row_sums.fill(0);
        const std::uint8_t* row = pixels + y * width;
        for (std::size_t x = 0; x < width; ++x) row_sums[col_block[x]] += row[x];
        const std::size_t base = row_block[y] * kCols;
        for (std::size_t c = 0; c < kCols; ++c) sums[base + c] += row_sums[c];
    }

    std::uint64_t hash = 0;
    for (std::size_t r = 0; r < kRows; ++r) {
        for (std::size_t c = 0; c + 1 < kCols; ++c) {
            const std::uint64_t left_count = row_count[r] * col_count[c];
            const std::uint64_t right_count = row_count[r] * col_count[c + 1];
            const bool right_brighter = sums[r * kCols + c + 1] * left_count > sums[r * kCols + c] * right_count;
            hash = (hash << 1) | static_cast<std::uint64_t>(right_brighter);
        }
    }
    return hash;
}

}  // namespace banana
