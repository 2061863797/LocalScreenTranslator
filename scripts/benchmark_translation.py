"""本地模型推理基准；不下载模型，也不写入用户历史或缓存。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DEFAULTS
from app.storage import Storage
from app.translation_runtime.router import TranslationRouter


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("page_fault_count", ctypes.c_uint32),
        ("peak_working_set", ctypes.c_size_t),
        ("working_set", ctypes.c_size_t),
        ("quota_peak_paged_pool", ctypes.c_size_t),
        ("quota_paged_pool", ctypes.c_size_t),
        ("quota_peak_nonpaged_pool", ctypes.c_size_t),
        ("quota_nonpaged_pool", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def _working_set_mib() -> float | None:
    if os.name != "nt":
        return None
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = ctypes.c_void_p
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    read = ctypes.windll.psapi.GetProcessMemoryInfo
    read.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
    read.restype = ctypes.c_int
    if not read(process(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.working_set / 1048576, 1)


def _gpu_memory_mib() -> int | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    total = 0
    found = False
    for line in result.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) == 2 and fields[0] == str(os.getpid()) and fields[1].isdigit():
            total += int(fields[1])
            found = True
    return total if found else None


def _gpu_total_used_mib() -> int | None:
    """WDDM 不提供进程显存时，只记录整卡占用供人工对照。"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = [line.strip() for line in result.stdout.splitlines()]
    return sum(int(value) for value in values) if values and all(value.isdigit() for value in values) else None


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", choices=("auto", "cpu", "gpu"), default="auto")
    parser.add_argument("--source", default="英语")
    parser.add_argument("--target", default="简体中文")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 2:
        parser.error("--repeat 至少为 2，才能计算热态分位数")

    cfg = dict(DEFAULTS)
    cfg.update(
        model_path=str(args.model),
        llama_device=args.device,
        source_language=args.source,
        translation_cache_enabled=False,
    )
    inputs = (
        "The train arrives at eight o'clock.",
        "The weather will improve tomorrow.",
        "Please save the document before closing.",
        "This station is closed after midnight.",
        "The meeting starts in ten minutes.",
        "Turn left at the next street.",
    )
    with tempfile.TemporaryDirectory() as directory:
        storage = Storage(Path(directory) / "benchmark.db")
        router = TranslationRouter(cfg, storage)
        try:
            before = _working_set_mib()
            gpu_total_before = _gpu_total_used_mib()
            started = time.perf_counter()
            router.preload()
            load_seconds = time.perf_counter() - started
            loaded = _working_set_mib()
            gpu_loaded = _gpu_memory_mib()
            gpu_total_loaded = _gpu_total_used_mib()
            durations: list[float] = []
            samples: list[str] = []
            for index in range(args.repeat + 1):
                text = inputs[index % len(inputs)]
                started = time.perf_counter()
                value = router.translate(text, args.target, "benchmark")
                durations.append(round(time.perf_counter() - started, 3))
                if not value:
                    raise RuntimeError(f"第 {index + 1} 次翻译为空")
                samples.append(value)
            warm = durations[1:]
            report = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "backend": "llama.cpp",
                "device": args.device,
                "source": args.source,
                "target": args.target,
                "samples": len(durations),
                "model_load_seconds": round(load_seconds, 3),
                "first_sentence_seconds": durations[0],
                "warm_p50_seconds": _percentile(warm, 0.5),
                "warm_p95_seconds": _percentile(warm, 0.95),
                "warm_each_seconds": warm,
                "working_set_before_mib": before,
                "working_set_loaded_mib": loaded,
                "working_set_after_mib": _working_set_mib(),
                "gpu_memory_loaded_mib": gpu_loaded,
                "gpu_memory_after_mib": _gpu_memory_mib(),
                "gpu_total_used_before_mib": gpu_total_before,
                "gpu_total_used_loaded_mib": gpu_total_loaded,
                "gpu_total_used_after_mib": _gpu_total_used_mib(),
                "translations": samples,
            }
        finally:
            router.close()
            storage.close()
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
