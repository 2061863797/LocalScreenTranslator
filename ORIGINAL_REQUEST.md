# Original User Request

## 2026-09-20T14:03:59Z

Execute the comprehensive 5-phase architectural refactoring and performance upgrade for LocalScreenTranslator based on the technical design specification. Transform the software from a fixed-interval polling tool into a modular, event-driven, low-latency, latest-frame-wins real-time screen translation engine with zero-flicker UI and DXGI/WGC capture abstractions across 12 PR-grade incremental steps.

Working directory: d:\ScreenTranslator
Integrity mode: development

## Requirements

### R1. Phase 1: Visual Experience & Correctness (PR 1, PR 2)
- Zero-Flicker Subtitle Overlay: Completely eliminate startup placeholder text (e.g. "Starting watch"). The subtitle overlay must remain hidden until the very first valid translation is produced, and must complete full layout/chrome calculation before DWM shows the window.
- Generation Tracking: Assign a monotonically increasing generation_id to each OCR/translation cycle. Drop any asynchronous translation response whose generation ID does not match the active generation, guaranteeing older translations never overwrite newer text.

### R2. Phase 2: Lightweight Visual Change & ROI OCR (PR 3, PR 4, PR 5, PR 6)
- FrameChangeDetector: Implement a two-stage frame change detector using downsampled grayscale difference (absdiff) and thresholding to filter out video noise, cursor blinking, and static frames (< 3ms cost).
- Adaptive Polling: Replace static intervals with an adaptive state machine transitioning through ACTIVE (~120ms), WARM (~250ms), IDLE (~500ms), and DEEP_IDLE (~800ms) states.
- ROI OCR & Fallback: Compute bounding box for changed pixels with safe margin padding to run localized OCR on active regions, with automatic fallback to full OCR when ROI area ratio > 0.6 or on periodic health checks.
- OCR Stabilizer: Implement stabilization logic to prevent OCR jitter (e.g., character flipping across adjacent frames) from triggering redundant LLM translation requests.

### R3. Phase 3: Incremental Translation & Unified Caching (PR 7, PR 8)
- Subtitle Incremental Translation: Unify subtitle translation with line-diff units so unchanged lines are reused directly from cache, submitting only genuinely changed lines to the translation model.
- Persistent Translation Cache: Establish a two-tier caching architecture (L1 in-memory LRU + L2 SQLite translation_cache table) with composite cache keys (normalized source hash, target language, model ID, prompt version) and LRU eviction (cap at 50,000 entries).

### R4. Phase 4: Core Pipeline Architectural Decoupling (PR 9, PR 11)
- Decouple WindowWatcher: Split WindowWatcher into dedicated single-responsibility components: CaptureService, FrameChangeDetector, OcrService, TextChangeDetector, TranslationManager, and ResultManager.
- Latest-Frame-Wins Engine: Implement a single-capacity LatestFrameBuffer to decouple high-frequency screen capture from compute-intensive OCR/LLM inference, dropping intermediate stale frames when inference is busy.

### R5. Phase 5: Native Window Consolidation, Capture Backends & Metrics (PR 10, PR 12)
- Single HWND Subtitle Overlay: Consolidate SubtitleBar, controls, scrollbar, and resize grip into a single top-level native window with child Qt widgets, eliminating multi-HWND Z-order fighting and DWM composition friction.
- CaptureBackend Abstraction: Abstract screen capture into CaptureBackend interface supporting existing Win32/MSS compatibility backends while providing extensible hooks for DXGI / Windows Graphics Capture (WGC).
- History Queue Asynchrony: Offload history recording to an asynchronous background queue with batch SQLite commits, ensuring database I/O never delays real-time subtitle delivery.
- Pipeline Metrics & Benchmark Suite: Instrument PipelineMetrics capturing per-stage latencies (capture, frame_diff, ocr, text_diff, cache, translate, ui) and implement scripts/benchmark_live_pipeline.py.

## Acceptance Criteria

### Visual & Overlay Behavior
- [ ] Subtitle window is completely hidden during startup and watch initialization until the first valid translation result is available.
- [ ] Subtitle controls, scrollbar, and resize grip share exactly one native top-level HWND.
- [ ] Continuous subtitle updates do not recreate native windows or cause perceptible visual layout jitter.

### Correctness & Concurrency
- [ ] Simulated out-of-order responses (Result #1 arriving after Result #2) result in only Result #2 being rendered on UI.
- [ ] When capture rate exceeds inference rate, LatestFrameBuffer drops intermediate frames and processes only the most recent frame.
- [ ] History database writes run asynchronously and never block or delay subtitle frame updates.

### Performance & Caching
- [ ] Static screen/window frames skip OCR on >= 95% of checks during idle testing.
- [ ] Frame change detection executes in < 3ms on typical 1080p frames.
- [ ] L1 memory cache hits return in < 1ms; L2 SQLite cache hits resolve in < 10ms without invoking llama-server.
- [ ] Subtitle line updates translate only modified lines instead of the entire block.

### Quality, Testing & Regression
- [ ] All existing 94 unit tests in tests/ pass with exit code 0 (python -m unittest discover -s tests).
- [ ] New unit and integration test suites cover:
  - test_frame_change.py (frame diff, noise filtering, ROI bounds)
  - test_generation.py (stale translation drop, out-of-order handling)
  - test_subtitle_overlay.py (first-show visibility, single HWND)
  - test_translation_cache.py (L1/L2 hits, eviction, cache key invalidation)
  - test_pipeline_metrics.py (metrics collection and reporting)
- [ ] scripts/benchmark_live_pipeline.py executes successfully and prints latency breakdowns for each pipeline stage.

## 2026-09-20T22:03:42Z

服务器已重启并恢复会话，用户要求继续推进。请检查当前编排器与各子任务状态，恢复巡检守护，并继续执行 Milestone 4/5 剩余工作直至胜果审计结项。
