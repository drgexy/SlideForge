#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import math
import mmap
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import av
import numpy as np
import tifffile


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SdpcHeader:
    src_width: int
    src_height: int
    slice_width: int
    slice_height: int
    hierarchy: int
    scale: float
    ruler: float
    rate: int
    file_size: int


@dataclass
class SdpcLevel:
    index: int
    record_offset: int
    tile_count: int
    tiles_x: int
    tiles_y: int
    size_table_offset: int
    data_offset: int
    data_end: int
    sizes: list[int]

    @property
    def width(self) -> int:
        return self.tiles_x * self.tile_size

    @property
    def height(self) -> int:
        return self.tiles_y * self.tile_size

    tile_size: int = 672


def parse_header(data: bytes) -> SdpcHeader:
    if data[:2] != b"SQ":
        raise RuntimeError("not an SDPC file: missing SQ magic")
    return SdpcHeader(
        src_width=struct.unpack_from("<I", data, 42)[0],
        src_height=struct.unpack_from("<I", data, 46)[0],
        slice_width=struct.unpack_from("<I", data, 50)[0],
        slice_height=struct.unpack_from("<I", data, 54)[0],
        hierarchy=struct.unpack_from("<I", data, 38)[0],
        scale=struct.unpack_from("<f", data, 72)[0],
        ruler=struct.unpack_from("<d", data, 76)[0],
        rate=struct.unpack_from("<I", data, 84)[0],
        file_size=struct.unpack_from("<q", data, 22)[0],
    )


def parse_sdpc_levels(data: bytes, header: SdpcHeader) -> list[SdpcLevel]:
    first = data.find(b"IFz", 128 * 1024)
    while first >= 0:
        layer = struct.unpack_from("<I", data, first + 6)[0]
        count = struct.unpack_from("<I", data, first + 10)[0]
        nx = struct.unpack_from("<I", data, first + 14)[0]
        ny = struct.unpack_from("<I", data, first + 18)[0]
        if layer == 1 and count > 0 and count == nx * ny:
            break
        first = data.find(b"IFz", first + 1)
    if first < 0:
        raise RuntimeError("could not locate first IFz tile layer record")

    levels: list[SdpcLevel] = []
    offset = first
    while offset < len(data):
        if data[offset : offset + 3] != b"IFz":
            raise RuntimeError(f"expected IFz at byte {offset}, found {data[offset:offset+8].hex()}")
        layer = struct.unpack_from("<I", data, offset + 6)[0]
        count = struct.unpack_from("<I", data, offset + 10)[0]
        nx = struct.unpack_from("<I", data, offset + 14)[0]
        ny = struct.unpack_from("<I", data, offset + 18)[0]
        if count != nx * ny:
            raise RuntimeError(f"invalid tile count at layer record {offset}: {count} != {nx}*{ny}")
        table = offset + 122
        sizes = [struct.unpack_from("<I", data, table + idx * 4)[0] for idx in range(count)]
        bad = [value for value in sizes if value <= 0 or value > 1_000_000]
        if bad:
            raise RuntimeError(f"implausible tile sizes in layer {layer}: first bad value {bad[0]}")
        data_offset = table + count * 4
        data_end = data_offset + sum(sizes)
        levels.append(
            SdpcLevel(
                index=layer - 1,
                record_offset=offset,
                tile_count=count,
                tiles_x=nx,
                tiles_y=ny,
                size_table_offset=table,
                data_offset=data_offset,
                data_end=data_end,
                sizes=sizes,
                tile_size=header.slice_width,
            )
        )
        offset = data_end
        if len(levels) >= header.hierarchy:
            break
    if len(levels) != header.hierarchy:
        raise RuntimeError(f"expected {header.hierarchy} levels, parsed {len(levels)}")
    return levels


def slide_metadata(header: SdpcHeader, levels: list[SdpcLevel], source: Path) -> dict[str, Any]:
    return {
        "source": str(source),
        "level_count": len(levels),
        "level_dimensions": [[level.width, level.height] for level in levels],
        "level_downsamples": [float((1.0 / header.scale) ** level.index) if header.scale else float(2**level.index) for level in levels],
        "tile_counts": [level.tile_count for level in levels],
        "tile_grids": [[level.tiles_x, level.tiles_y] for level in levels],
        "sdpc_src_width": header.src_width,
        "sdpc_src_height": header.src_height,
        "mpp_um_per_pixel": float(header.ruler) if header.ruler else None,
        "scale": float(header.scale),
        "objective_power": int(header.rate),
    }


def tiff_resolution(mpp_um_per_pixel: float | None) -> tuple[tuple[float, float] | None, str | None]:
    if not mpp_um_per_pixel or mpp_um_per_pixel <= 0:
        return None, None
    pixels_per_cm = 10000.0 / float(mpp_um_per_pixel)
    return (pixels_per_cm, pixels_per_cm), "CENTIMETER"


def decode_hevc_tile(blob: bytes) -> np.ndarray:
    container = av.open(io.BytesIO(blob), format="hevc")
    frame = None
    for frame in container.decode(video=0):
        pass
    container.close()
    if frame is None:
        raise RuntimeError("HEVC tile did not decode to a frame")
    arr = frame.to_ndarray(format="rgb24")
    return np.ascontiguousarray(arr, dtype=np.uint8)


def tile_blob(data: bytes, level: SdpcLevel, tile_index: int) -> bytes:
    offset = level.data_offset + sum(level.sizes[:tile_index])
    return data[offset : offset + level.sizes[tile_index]]


def tile_iter(data: bytes, level: SdpcLevel, progress_every: int = 250) -> Iterable[np.ndarray]:
    offset = level.data_offset
    for idx, size in enumerate(level.sizes):
        yield decode_hevc_tile(data[offset : offset + size])
        offset += size
        if progress_every and (idx + 1) % progress_every == 0:
            print(f"[{now()}] level={level.index} tiles={idx + 1}/{level.tile_count}", flush=True)


def write_pyramidal_tiff(
    data: bytes,
    parsed_levels: list[SdpcLevel],
    out_path: Path,
    *,
    levels: list[int],
    compression: str | None,
    jpeg_quality: int,
    mpp_um_per_pixel: float | None,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resolution, resolutionunit = tiff_resolution(mpp_um_per_pixel)
    compressionargs = {"level": jpeg_quality} if compression == "jpeg" else None
    with tifffile.TiffWriter(out_path, bigtiff=True) as tif:
        base = parsed_levels[levels[0]]
        tile_size = base.tile_size
        tif.write(
            tile_iter(data, base),
            shape=(base.height, base.width, 3),
            dtype=np.uint8,
            photometric="rgb",
            tile=(tile_size, tile_size),
            compression=compression,
            compressionargs=compressionargs,
            resolution=resolution,
            resolutionunit=resolutionunit,
            metadata=None,
            description=f"Aperio Image Library compatible\nMPP = {mpp_um_per_pixel or ''}\nConverted from SDPC by convert_sdpc_to_pyramidal_tiff.py",
        )
        for level_index in levels[1:]:
            level = parsed_levels[level_index]
            level_downsample = float(2 ** (level.index - base.index))
            level_resolution = None
            if resolution is not None:
                level_resolution = (resolution[0] / level_downsample, resolution[1] / level_downsample)
            tif.write(
                tile_iter(data, level),
                shape=(level.height, level.width, 3),
                dtype=np.uint8,
                photometric="rgb",
                tile=(tile_size, tile_size),
                compression=compression,
                compressionargs=compressionargs,
                subfiletype=1,
                resolution=level_resolution,
                resolutionunit=resolutionunit,
                metadata=None,
                description=f"Aperio Image Library compatible\nMPP = {mpp_um_per_pixel or ''}\nConverted from SDPC by convert_sdpc_to_pyramidal_tiff.py",
            )


def parse_level_selection(raw: str, level_count: int) -> list[int]:
    text = raw.strip().lower()
    if text == "all":
        return list(range(level_count))
    levels = sorted({int(item) for item in text.split(",") if item.strip()})
    bad = [item for item in levels if item < 0 or item >= level_count]
    if bad:
        raise ValueError(f"invalid levels {bad}; available range is 0..{level_count - 1}")
    return levels


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert SDPC to tiled pyramidal BigTIFF natively on macOS.")
    parser.add_argument("sdpc_path", type=Path)
    parser.add_argument("out_tiff", type=Path)
    parser.add_argument("--levels", default="all", help="all or comma-separated levels, must include 0")
    parser.add_argument("--compression", choices=["jpeg", "deflate", "none"], default="jpeg")
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--mpp", type=float, default=0.0, help="override microns per pixel for level 0")
    args = parser.parse_args()

    if args.compression == "jpeg":
        import imagecodecs  # noqa: F401

    with args.sdpc_path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            header = parse_header(data)
            parsed = parse_sdpc_levels(data, header)
            levels = parse_level_selection(args.levels, len(parsed))
            mpp = args.mpp if args.mpp > 0 else header.ruler
            compression = None if args.compression == "none" else args.compression
            meta = slide_metadata(header, parsed, args.sdpc_path)
            meta.update(
                {
                    "started_at": now(),
                    "out_tiff": str(args.out_tiff),
                    "tile_size": header.slice_width,
                    "written_levels": levels,
                    "compression": args.compression,
                    "jpeg_quality": args.jpeg_quality if args.compression == "jpeg" else None,
                    "decoder": "mac-native-pyav-hevc",
                    "reader": "memory-mapped-sdpc",
                }
            )
            sidecar = args.out_tiff.with_suffix(args.out_tiff.suffix + ".conversion.json")
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
            write_pyramidal_tiff(
                data,
                parsed,
                args.out_tiff,
                levels=levels,
                compression=compression,
                jpeg_quality=args.jpeg_quality,
                mpp_um_per_pixel=mpp,
            )
            meta["finished_at"] = now()
            meta["output_size_bytes"] = args.out_tiff.stat().st_size
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"ok": True, "out_tiff": str(args.out_tiff), "sidecar": str(sidecar)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
