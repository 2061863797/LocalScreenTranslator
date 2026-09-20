# -*- coding: utf-8 -*-
"""全链路性能指标遥测与阶段耗时采集器 (PR 12).

定义 7 大核心管线阶段：
- capture: 屏幕/窗口抓取；
- frame_diff: 两阶段轻量画面差分与噪点判定；
- ocr: ROI 局部识别与全图 OCR；
- text_diff: 文本变化观察与跳过判定；
- cache_lookup: L1 内存 + L2 SQLite 译文缓存检索；
- translate: 本地 llama 模型推理或增量行级翻译；
- ui_render: Qt UI 信号派发与排版渲染。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class PipelineStageTimers:
    """单帧全管线各阶段耗时容器 (毫秒)."""

    capture_ms: float = 0.0
    frame_diff_ms: float = 0.0
    ocr_ms: float = 0.0
    text_diff_ms: float = 0.0
    cache_lookup_ms: float = 0.0
    translate_ms: float = 0.0
    ui_render_ms: float = 0.0
    total_ms: float = 0.0


class PipelineMetrics:
    """全管线分阶段微秒级耗时指标采集器 (PR 12)."""

    STAGES = [
        "capture",
        "frame_diff",
        "ocr",
        "text_diff",
        "cache_lookup",
        "translate",
        "ui_render",
    ]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[float]] = {s: [] for s in self.STAGES}

    def record_stage(self, stage: str, duration_ms: float) -> None:
        """记录指定阶段的耗时 (毫秒)."""
        with self._lock:
            if stage not in self._records:
                self._records[stage] = []
            self._records[stage].append(float(duration_ms))

    @contextmanager
    def stage_timer(self, stage: str) -> Iterator[None]:
        """测量代码块耗时的上下文管理器."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dur = (time.perf_counter() - t0) * 1000.0
            self.record_stage(stage, dur)

    measure = stage_timer

    def reset(self) -> None:
        """清空所有阶段已记录的样本."""
        with self._lock:
            self._records = {s: [] for s in self.STAGES}

    def get_stats(self) -> dict[str, dict[str, float]]:
        """计算各阶段统计指标 (count, avg, p50, p95, max)."""
        with self._lock:
            stats = {}
            records = getattr(self, "_records", None)
            if records is None:
                records = getattr(self, "_samples", {})
            all_stages = list(self.STAGES) + [s for s in sorted(records.keys()) if s not in self.STAGES]
            for stage in all_stages:
                vals = records.get(stage, [])
                if not vals:
                    stats[stage] = {
                        "count": 0,
                        "avg": 0.0,
                        "p50": 0.0,
                        "p95": 0.0,
                        "max": 0.0,
                    }
                    continue
                sorted_vals = sorted(vals)
                n = len(sorted_vals)
                avg = sum(sorted_vals) / n
                p50 = sorted_vals[int(n * 0.50)]
                p95 = sorted_vals[min(int(n * 0.95), n - 1)]
                max_val = sorted_vals[-1]
                stats[stage] = {
                    "count": n,
                    "avg": round(avg, 3),
                    "p50": round(p50, 3),
                    "p95": round(p95, 3),
                    "max": round(max_val, 3),
                }
            return stats

    get_summary = get_stats

    def format_table(self) -> str:
        """生成格式化 ASCII 表格."""
        stats = self.get_stats()
        lines = []
        lines.append("=" * 104)
        lines.append(
            f"{'STAGE':<22}{'SAMPLES':>10}{'AVG (ms)':>14}{'P50 (ms)':>14}{'P95 (ms)':>14}{'MAX (ms)':>14}{'RATIO':>12}"
        )
        lines.append("=" * 104)

        total_avg = sum(s["avg"] for s in stats.values())
        total_samples = max((s["count"] for s in stats.values()), default=0)

        all_stages = list(self.STAGES) + [s for s in sorted(stats.keys()) if s not in self.STAGES]
        for stage in all_stages:
            s = stats[stage]
            ratio_str = (
                f"{(s['avg'] / total_avg * 100):.1f}%" if total_avg > 0 else "0.0%"
            )
            lines.append(
                f"{stage:<22}{s['count']:>10}{s['avg']:>14.2f}{s['p50']:>14.2f}{s['p95']:>14.2f}{s['max']:>14.2f}{ratio_str:>12}"
            )
        lines.append("-" * 104)

        # Total pipeline row
        total_p50 = sum(s["p50"] for s in stats.values())
        total_p95 = sum(s["p95"] for s in stats.values())
        total_max = sum(s["max"] for s in stats.values())
        lines.append(
            f"{'TOTAL PIPELINE':<22}{total_samples:>10}{total_avg:>14.2f}{total_p50:>14.2f}{total_p95:>14.2f}{total_max:>14.2f}{'100.0%':>12}"
        )
        lines.append("=" * 104)

        fps = 1000.0 / total_avg if total_avg > 0 else 0.0
        lines.append(
            f"Throughput: {fps:.2f} FPS | P95 Pipeline Latency: {total_p95:.2f} ms"
        )
        return "\n".join(lines)
