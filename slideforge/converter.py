#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
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


def make_svs_entry(out_tif: Path) -> Path:
    name = out_tif.name.removesuffix(".ome.tif")
    svs = out_tif.with_name(f"{name}.svs")
    if svs.exists() or svs.is_symlink():
        svs.unlink()
    try:
        svs.hardlink_to(out_tif)
    except OSError:
        svs.symlink_to(out_tif.name)
    return svs


def infer_single_output(sdpc_path: Path, output_path: Path, output_format: str) -> tuple[Path, str]:
    fmt = output_format
    if output_path.exists() and output_path.is_dir():
        fmt = "both" if fmt == "auto" else fmt
        suffix = ".svs" if fmt == "svs" else ".ome.tif"
        return output_path / f"{sdpc_path.stem}{suffix}", fmt
    if output_path.suffix.lower() == ".svs":
        return output_path, "svs" if fmt == "auto" else fmt
    fmt = "ome.tif" if fmt == "auto" else fmt
    if fmt == "svs":
        name = output_path.name.removesuffix(".ome.tif")
        if name == output_path.name:
            name = output_path.stem
        return output_path.with_name(f"{name}.svs"), fmt
    return output_path, fmt


def iter_sdpc_files(input_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*.sdpc" if recursive else "*.sdpc"
    return sorted(path for path in input_dir.glob(pattern) if path.is_file())


def convert_sdpc_file(
    sdpc_path: Path,
    out_tiff: Path,
    *,
    level_selection: str = "all",
    compression_name: str = "jpeg",
    jpeg_quality: int = 90,
    mpp_override: float = 0.0,
    output_format: str = "ome.tif",
    skip_existing: bool = False,
) -> dict[str, Any]:
    if skip_existing and out_tiff.exists():
        return {"ok": True, "skipped": True, "out_tiff": str(out_tiff)}

    if compression_name == "jpeg":
        import imagecodecs  # noqa: F401

    with sdpc_path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            header = parse_header(data)
            parsed = parse_sdpc_levels(data, header)
            levels = parse_level_selection(level_selection, len(parsed))
            mpp = mpp_override if mpp_override > 0 else header.ruler
            compression = None if compression_name == "none" else compression_name
            meta = slide_metadata(header, parsed, sdpc_path)
            meta.update(
                {
                    "started_at": now(),
                    "out_tiff": str(out_tiff),
                    "tile_size": header.slice_width,
                    "written_levels": levels,
                    "compression": compression_name,
                    "jpeg_quality": jpeg_quality if compression_name == "jpeg" else None,
                    "decoder": "mac-native-pyav-hevc",
                    "reader": "memory-mapped-sdpc",
                }
            )
            sidecar = out_tiff.with_suffix(out_tiff.suffix + ".conversion.json")
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
            write_pyramidal_tiff(
                data,
                parsed,
                out_tiff,
                levels=levels,
                compression=compression,
                jpeg_quality=jpeg_quality,
                mpp_um_per_pixel=mpp,
            )
            meta["finished_at"] = now()
            meta["output_size_bytes"] = out_tiff.stat().st_size
            svs_path = None
            if output_format == "both":
                svs_path = make_svs_entry(out_tiff)
                meta["svs"] = str(svs_path)
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result = {"ok": True, "out_tiff": str(out_tiff), "sidecar": str(sidecar)}
            if svs_path is not None:
                result["svs"] = str(svs_path)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result


def convert_input_path(
    input_path: Path,
    output_path: Path,
    *,
    level_selection: str,
    compression_name: str,
    jpeg_quality: int,
    mpp_override: float,
    output_format: str,
    recursive: bool,
    skip_existing: bool,
) -> int:
    if input_path.is_dir():
        output_path.mkdir(parents=True, exist_ok=True)
        sdpc_files = iter_sdpc_files(input_path, recursive)
        if not sdpc_files:
            raise RuntimeError(f"no .sdpc files found in {input_path}")
        failures = []
        for index, sdpc_file in enumerate(sdpc_files, start=1):
            rel = sdpc_file.relative_to(input_path)
            out_dir = output_path / rel.parent
            fmt = "both" if output_format == "auto" else output_format
            suffix = ".svs" if fmt == "svs" else ".ome.tif"
            out_tif = out_dir / f"{sdpc_file.stem}{suffix}"
            print(f"[{now()}] batch={index}/{len(sdpc_files)} source={sdpc_file}", flush=True)
            try:
                convert_sdpc_file(
                    sdpc_file,
                    out_tif,
                    level_selection=level_selection,
                    compression_name=compression_name,
                    jpeg_quality=jpeg_quality,
                    mpp_override=mpp_override,
                    output_format=fmt,
                    skip_existing=skip_existing,
                )
            except Exception as exc:
                failures.append({"source": str(sdpc_file), "error": str(exc)})
                print(json.dumps({"ok": False, "source": str(sdpc_file), "error": str(exc)}, ensure_ascii=False), flush=True)
        summary = {"ok": not failures, "total": len(sdpc_files), "failed": len(failures), "failures": failures}
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 1 if failures else 0

    out_tif, fmt = infer_single_output(input_path, output_path, output_format)
    convert_sdpc_file(
        input_path,
        out_tif,
        level_selection=level_selection,
        compression_name=compression_name,
        jpeg_quality=jpeg_quality,
        mpp_override=mpp_override,
        output_format=fmt,
        skip_existing=skip_existing,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert one SDPC file or a directory of SDPC files to OpenSlide-compatible WSI outputs.")
    parser.add_argument("input_path", type=Path, help="input .sdpc file or directory containing .sdpc files")
    parser.add_argument("output_path", type=Path, help="output .ome.tif/.svs file for one input, or output directory for batch input")
    parser.add_argument("--levels", default="all", help="all or comma-separated levels, must include 0")
    parser.add_argument("--compression", choices=["jpeg", "deflate", "none"], default="jpeg")
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--mpp", type=float, default=0.0, help="override microns per pixel for level 0")
    parser.add_argument("--output-format", choices=["auto", "ome.tif", "svs", "both"], default="auto", help="output type; auto uses .ome.tif for file outputs and both for directory outputs")
    parser.add_argument("--recursive", action="store_true", help="batch-convert .sdpc files in subdirectories")
    parser.add_argument("--skip-existing", action="store_true", help="skip outputs that already exist")
    args = parser.parse_args()
    return convert_input_path(
        args.input_path,
        args.output_path,
        level_selection=args.levels,
        compression_name=args.compression,
        jpeg_quality=args.jpeg_quality,
        mpp_override=args.mpp,
        output_format=args.output_format,
        recursive=args.recursive,
        skip_existing=args.skip_existing,
    )


if __name__ == "__main__":
    raise SystemExit(main())
