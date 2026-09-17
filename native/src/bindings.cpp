#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>

#include <tuple>

#include "core.hpp"

namespace nb = nanobind;

using GrayArray = nb::ndarray<const std::uint8_t, nb::ndim<2>, nb::c_contig, nb::device::cpu>;

NB_MODULE(_banana_core, m) {
    m.doc() = "Native per-image hot paths for Photo Scanner";

    m.def(
        "dhash",
        [](const GrayArray& gray) {
            const std::uint8_t* data = gray.data();
            const std::size_t h = gray.shape(0), w = gray.shape(1);
            nb::gil_scoped_release release;
            return banana::dhash(data, h, w);
        },
        nb::arg("gray"));

    m.def(
        "blank_metrics",
        [](const GrayArray& gray, int edge_threshold) {
            const std::uint8_t* data = gray.data();
            const std::size_t h = gray.shape(0), w = gray.shape(1);
            banana::BlankMetrics r{};
            {
                nb::gil_scoped_release release;
                r = banana::blank_metrics(data, h, w, edge_threshold);
            }
            return std::make_tuple(r.mean, r.std, r.edge_density);
        },
        nb::arg("gray"), nb::arg("edge_threshold") = 24);

    using RgbArray = nb::ndarray<const std::uint8_t, nb::shape<-1, -1, 3>, nb::c_contig, nb::device::cpu>;
    m.def(
        "photo_bbox",
        [](const RgbArray& rgb) -> nb::object {
            const std::uint8_t* data = rgb.data();
            const std::size_t h = rgb.shape(0), w = rgb.shape(1);
            banana::Box box{};
            bool found;
            {
                nb::gil_scoped_release release;
                found = banana::photo_bbox(data, h, w, box);
            }
            if (!found) return nb::none();
            return nb::make_tuple(box.x0, box.y0, box.x1, box.y1);
        },
        nb::arg("rgb"));

#ifdef BANANA_HAVE_OPENCV
    m.attr("HAVE_OPENCV") = true;
    m.def(
        "decode_gray",
        [](const std::string& path, int max_side) {
            auto* img = new banana::GrayImage();
            try {
                nb::gil_scoped_release release;
                *img = banana::decode_gray(path, max_side);
            } catch (...) {
                delete img;
                throw;
            }
            nb::capsule owner(img, [](void* p) noexcept { delete static_cast<banana::GrayImage*>(p); });
            return nb::ndarray<nb::numpy, std::uint8_t, nb::ndim<2>>(
                img->pixels.data(), {img->height, img->width}, owner);
        },
        nb::arg("path"), nb::arg("max_side") = 1024);
#else
    m.attr("HAVE_OPENCV") = false;
#endif
}
