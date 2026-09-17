#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace banana {

// Must match banana/core/reference.py bit-for-bit.
std::uint64_t dhash(const std::uint8_t* pixels, std::size_t height, std::size_t width);

struct BlankMetrics {
    double mean;
    double std;
    double edge_density;
};

BlankMetrics blank_metrics(const std::uint8_t* pixels, std::size_t height, std::size_t width, int edge_threshold);

struct Box {
    std::size_t x0, y0, x1, y1;
};

// Photo rectangle on a sheet-fed scan (RGB, interleaved). Must match reference.photo_bbox exactly.
bool photo_bbox(const std::uint8_t* rgb, std::size_t height, std::size_t width, Box& out);

#ifdef BANANA_HAVE_OPENCV
struct GrayImage {
    std::vector<std::uint8_t> pixels;
    std::size_t height = 0;
    std::size_t width = 0;
};

GrayImage decode_gray(const std::string& path, int max_side);
#endif

}  // namespace banana
