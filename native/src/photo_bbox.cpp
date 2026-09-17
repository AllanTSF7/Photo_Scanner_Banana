#include <algorithm>
#include <vector>

#include "core.hpp"

namespace banana {
namespace {

// [start, end) of the longest run of true values, bridging gaps of up to max_gap false values.
bool longest_run(const std::vector<bool>& mask, std::size_t max_gap, std::size_t& best_start, std::size_t& best_end) {
    bool found = false;
    bool open = false;
    std::size_t start = 0;
    std::size_t last_true = 0;
    best_start = best_end = 0;
    for (std::size_t i = 0; i < mask.size(); ++i) {
        if (!mask[i]) continue;
        if (!open || i - last_true - 1 > max_gap) {
            start = i;
            open = true;
        }
        last_true = i;
        if (!found || (last_true + 1 - start) > (best_end - best_start)) {
            best_start = start;
            best_end = last_true + 1;
            found = true;
        }
    }
    return found;
}

}  // namespace

bool photo_bbox(const std::uint8_t* rgb, std::size_t height, std::size_t width, Box& out) {
    if (height < 8 || width < 8) return false;
    auto px = [&](std::size_t y, std::size_t x, int c) { return static_cast<int>(rgb[(y * width + x) * 3 + c]); };

    // Pure white padding rows at the bottom (past the scanned length).
    std::size_t scan_end = height;
    while (scan_end > 0) {
        std::size_t white = 0;
        for (std::size_t x = 0; x < width; ++x) {
            white += px(scan_end - 1, x, 0) >= 250 && px(scan_end - 1, x, 1) >= 250 && px(scan_end - 1, x, 2) >= 250;
        }
        if (white * 100 < 98 * width) break;
        --scan_end;
    }
    if (scan_end == 0) return false;

    std::vector<std::size_t> row_backing(scan_end, 0), col_backing(width, 0);
    for (std::size_t y = 0; y < scan_end; ++y) {
        for (std::size_t x = 0; x < width; ++x) {
            const int r = px(y, x, 0), g = px(y, x, 1), b = px(y, x, 2);
            const int total = r + g + b;
            const int spread = std::max({r, g, b}) - std::min({r, g, b});
            const bool backing = total >= 510 && total <= 720 && b - r >= 4 && b - r <= 40 && spread <= 45;
            row_backing[y] += backing;
            col_backing[x] += backing;
        }
    }

    std::vector<bool> photo_rows(scan_end), photo_cols(width);
    for (std::size_t y = 0; y < scan_end; ++y) photo_rows[y] = row_backing[y] * 100 < 85 * width;
    for (std::size_t x = 0; x < width; ++x) photo_cols[x] = col_backing[x] * 100 < 85 * scan_end;

    std::size_t y0, y1, x0, x1;
    if (!longest_run(photo_rows, std::max<std::size_t>(1, scan_end / 33), y0, y1)) return false;
    if (!longest_run(photo_cols, std::max<std::size_t>(1, width / 33), x0, x1)) return false;
    if ((y1 - y0) * 10 < scan_end / 10 || (x1 - x0) * 10 < width / 10) return false;
    out = {x0, y0, x1, y1};
    return true;
}

}  // namespace banana
