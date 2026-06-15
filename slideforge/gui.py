#!/usr/bin/env python3
from __future__ import annotations

import json
import mmap
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk


APP_NAME = "SlideForge Mac"
CREATOR = "Created by Xinyu Ge"
REPO_ROOT = Path(os.environ.get("SLIDEFORGE_REPO_ROOT", Path.cwd())).resolve()
from slideforge.converter import decode_hevc_tile, parse_header, parse_sdpc_levels  # noqa: E402


class SlideForgeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("860x620")
        self.minsize(760, 540)
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.proc: subprocess.Popen[str] | None = None
        self.sdpc_var = tk.StringVar()
        self.out_dir_var = tk.StringVar(value=str(REPO_ROOT / "converted_wsi"))
        self.output_format_var = tk.StringVar(value="both")
        self.status_var = tk.StringVar(value="Select an SDPC slide to begin.")
        self.progress_var = tk.DoubleVar(value=0)
        self.preview_image: ImageTk.PhotoImage | None = None
        self._build_ui()
        self.after(100, self._drain_queue)

    def _build_ui(self) -> None:
        pad = {"padx": 18, "pady": 10}
        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text=APP_NAME, font=("Helvetica", 22, "bold")).pack(anchor="w")
        ttk.Label(header, text="Native macOS SDPC to OpenSlide-compatible WSI/SVS conversion").pack(anchor="w")
        ttk.Label(header, text=CREATOR).pack(anchor="w")

        body = ttk.Frame(self)
        body.pack(fill="x", **pad)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Input file or folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.sdpc_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(body, text="Browse File", command=self.pick_input).grid(row=0, column=2)
        ttk.Button(body, text="Browse Folder", command=self.pick_input_dir).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(body, text="Output folder").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(body, textvariable=self.out_dir_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(12, 0))
        ttk.Button(body, text="Browse", command=self.pick_out_dir).grid(row=1, column=2, pady=(12, 0))

        ttk.Label(body, text="Output format").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            body,
            textvariable=self.output_format_var,
            values=("both", "ome.tif", "svs"),
            state="readonly",
            width=12,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(12, 0))

        controls = ttk.Frame(self)
        controls.pack(fill="x", **pad)
        self.convert_btn = ttk.Button(controls, text="Convert to WSI", command=self.start_convert)
        self.convert_btn.pack(side="left")
        self.batch_btn = ttk.Button(controls, text="Batch Convert Folder", command=self.start_batch_convert)
        self.batch_btn.pack(side="left", padx=(10, 0))
        self.cancel_btn = ttk.Button(controls, text="Stop", command=self.cancel_convert, state="disabled")
        self.cancel_btn.pack(side="left", padx=10)
        ttk.Button(controls, text="Generate Safe Preview", command=self.start_preview).pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="Open Output Folder", command=self.open_out_dir).pack(side="left")

        preview_frame = ttk.Frame(self)
        preview_frame.pack(fill="x", **pad)
        ttk.Label(preview_frame, text="Safe Preview").pack(anchor="w")
        self.preview_label = ttk.Label(preview_frame)
        self.preview_label.pack(anchor="w", pady=(6, 0))

        progress = ttk.Frame(self)
        progress.pack(fill="x", **pad)
        ttk.Label(progress, textvariable=self.status_var).pack(anchor="w")
        ttk.Progressbar(progress, variable=self.progress_var, maximum=100).pack(fill="x", pady=(8, 0))

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, **pad)
        ttk.Label(log_frame, text="Conversion Log").pack(anchor="w")
        self.log = tk.Text(log_frame, height=16, wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

    def pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select an SDPC or WSI slide",
            filetypes=[
                ("SDPC", "*.sdpc"),
                ("SVS", "*.svs"),
                ("TIFF", "*.tif"),
                ("TIFF", "*.tiff"),
                ("All files", "*"),
            ],
        )
        if path:
            self.sdpc_var.set(path)
            self.status_var.set("Input slide selected.")

    def pick_input_dir(self) -> None:
        path = filedialog.askdirectory(title="Select folder containing SDPC slides")
        if path:
            self.sdpc_var.set(path)
            self.status_var.set("Input folder selected.")

    def pick_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.out_dir_var.set(path)

    def open_out_dir(self) -> None:
        out_dir = Path(self.out_dir_var.get()).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(out_dir)], check=False)

    def start_convert(self) -> None:
        sdpc = Path(self.sdpc_var.get()).expanduser()
        out_dir = Path(self.out_dir_var.get()).expanduser()
        if not sdpc.exists() or sdpc.suffix.lower() != ".sdpc":
            messagebox.showerror(APP_NAME, "Please select a valid .sdpc slide.")
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        output_format = self.output_format_var.get()
        suffix = ".svs" if output_format == "svs" else ".ome.tif"
        out_tif = out_dir / f"{sdpc.stem}{suffix}"
        self.log.delete("1.0", "end")
        self.progress_var.set(0)
        self.status_var.set("Starting conversion.")
        self._set_running(True)
        cmd = [
            sys.executable,
            "-m",
            "slideforge.converter",
            str(sdpc),
            str(out_tif),
            "--output-format",
            output_format,
            "--compression",
            "jpeg",
            "--jpeg-quality",
            "90",
        ]
        thread = threading.Thread(target=self._run_process, args=(cmd, out_tif, False), daemon=True)
        thread.start()

    def start_batch_convert(self) -> None:
        input_dir = Path(self.sdpc_var.get()).expanduser()
        out_dir = Path(self.out_dir_var.get()).expanduser()
        if not input_dir.exists() or not input_dir.is_dir():
            messagebox.showerror(APP_NAME, "Please select a valid folder containing .sdpc slides.")
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        self.log.delete("1.0", "end")
        self.progress_var.set(0)
        self.status_var.set("Starting batch conversion.")
        self._set_running(True)
        cmd = [
            sys.executable,
            "-m",
            "slideforge.converter",
            str(input_dir),
            str(out_dir),
            "--output-format",
            self.output_format_var.get(),
            "--compression",
            "jpeg",
            "--jpeg-quality",
            "90",
        ]
        thread = threading.Thread(target=self._run_process, args=(cmd, None, True), daemon=True)
        thread.start()

    def start_preview(self) -> None:
        path = Path(self.sdpc_var.get()).expanduser()
        out_dir = Path(self.out_dir_var.get()).expanduser()
        if not path.exists():
            messagebox.showerror(APP_NAME, "Please select a valid slide file.")
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        self.status_var.set("Generating safe preview.")
        thread = threading.Thread(target=self._run_preview, args=(path, out_dir), daemon=True)
        thread.start()

    def _run_preview(self, path: Path, out_dir: Path) -> None:
        try:
            if path.suffix.lower() == ".sdpc":
                out = self._preview_sdpc(path, out_dir)
            else:
                out = self._preview_wsi(path, out_dir)
            self.queue.put(("preview", str(out)))
        except Exception as exc:
            self.queue.put(("error", f"Safe preview failed: {exc}"))

    def _preview_sdpc(self, path: Path, out_dir: Path, max_side: int = 2048) -> Path:
        with path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                header = parse_header(data)
                levels = parse_sdpc_levels(data, header)
                level = levels[-1]
                canvas = Image.new("RGB", (level.width, level.height), "white")
                offset = level.data_offset
                for idx, size in enumerate(level.sizes):
                    tile = decode_hevc_tile(data[offset : offset + size])
                    offset += size
                    image = Image.fromarray(tile, mode="RGB")
                    x = (idx % level.tiles_x) * level.tile_size
                    y = (idx // level.tiles_x) * level.tile_size
                    canvas.paste(image, (x, y))
        canvas.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = out_dir / f"{path.stem}_sdpc_safe_preview.jpg"
        canvas.save(out, quality=92)
        return out

    def _preview_wsi(self, path: Path, out_dir: Path, max_side: int = 2048) -> Path:
        from openslide import OpenSlide

        slide = OpenSlide(str(path))
        try:
            image = slide.get_thumbnail((max_side, max_side)).convert("RGB")
        finally:
            slide.close()
        out = out_dir / f"{path.stem}_safe_preview.jpg"
        image.save(out, quality=92)
        return out

    def cancel_convert(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.status_var.set("Stopping conversion.")

    def _run_process(self, cmd: list[str], out_tif: Path | None, is_batch: bool) -> None:
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.queue.put(("line", line))
            code = self.proc.wait()
            if code == 0:
                if out_tif is not None and not is_batch:
                    self._make_preview(out_tif)
                self.queue.put(("done_batch" if is_batch else "done", str(out_tif or self.out_dir_var.get())))
            else:
                self.queue.put(("error", f"Conversion stopped or failed with exit code {code}."))
        except Exception as exc:
            self.queue.put(("error", str(exc)))

    def _make_preview(self, out_tif: Path) -> None:
        try:
            from openslide import OpenSlide
            slide = OpenSlide(str(out_tif))
            thumb = slide.get_thumbnail((2048, 2048)).convert("RGB")
            thumb.save(out_tif.with_name(f"{out_tif.stem}_safe_view.jpg"), quality=92)
            slide.close()
        except Exception as exc:
            self.queue.put(("line", f"Safe preview generation failed: {exc}\n"))

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, text = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._append_log(text)
                self._update_progress_from_line(text)
            elif kind == "done":
                self.progress_var.set(100)
                self.status_var.set("Conversion complete.")
                self._set_running(False)
                self._append_log(f"\nCompleted: {text}\n")
                messagebox.showinfo(APP_NAME, "Conversion complete. WSI/SVS output and a safe preview have been generated.")
            elif kind == "done_batch":
                self.progress_var.set(100)
                self.status_var.set("Batch conversion complete.")
                self._set_running(False)
                self._append_log(f"\nBatch completed: {text}\n")
                messagebox.showinfo(APP_NAME, "Batch conversion complete. Outputs have been written to the selected folder.")
            elif kind == "preview":
                self.status_var.set("Safe preview generated.")
                self._append_log(f"\nSafe preview: {text}\n")
                self._show_preview(Path(text))
                subprocess.run(["open", text], check=False)
            elif kind == "error":
                self.status_var.set("Not completed.")
                self._set_running(False)
                self._append_log(f"\n{text}\n")
                messagebox.showwarning(APP_NAME, text)
        self.after(100, self._drain_queue)

    def _show_preview(self, path: Path) -> None:
        image = Image.open(path).convert("RGB")
        image.thumbnail((420, 260), Image.Resampling.LANCZOS)
        self.preview_image = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self.preview_image)

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.convert_btn.configure(state=state)
        self.batch_btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def _update_progress_from_line(self, line: str) -> None:
        batch_match = re.search(r"batch=(\d+)/(\d+)", line)
        if batch_match:
            done = int(batch_match.group(1)) - 1
            total = int(batch_match.group(2))
            self.status_var.set(f"Batch converting slide {done + 1}/{total}")
            self.progress_var.set(min(95.0, done / max(total, 1) * 100.0))
            return
        match = re.search(r"level=(\d+) tiles=(\d+)/(\d+)", line)
        if not match:
            if '"level_count"' in line:
                self.status_var.set("Parsing SDPC slide structure.")
            return
        level = int(match.group(1))
        done = int(match.group(2))
        total = int(match.group(3))
        self.status_var.set(f"Converting pyramid level {level + 1}: {done}/{total} tiles")
        # Level 0 is the heavy part; keep progress honest without requiring full metadata parsing in the UI.
        if level == 0:
            self.progress_var.set(min(75.0, done / max(total, 1) * 75.0))
        else:
            self.progress_var.set(min(98.0, 75.0 + (level * 3.0) + done / max(total, 1) * 3.0))


if __name__ == "__main__":
    SlideForgeApp().mainloop()


def main() -> None:
    SlideForgeApp().mainloop()
