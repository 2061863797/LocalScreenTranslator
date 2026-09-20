# -*- coding: utf-8 -*-
"""基于 ONNX Runtime 的 PP-OCRv6 检测与识别。"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .applog import get_logger
from .paths import RUNTIME_OCR

_log = get_logger("ocr")

# 识别分批：目标桶容量与同批宽高比跨度上限。
# ONNX 每次调用有固定开销（本机实测约 30ms/轮），桶太碎会把开销放大；
# 同批比例差太大则 padding 浪费，故跨度设上限。manifest 的 batch_size
# 只当下限用：拆得太碎比多 padding 更慢。
_REC_TARGET_BATCH = 16
_REC_MAX_RATIO_SPREAD = 1.6


@dataclass
class OcrLine:
    text: str
    score: float
    box: tuple[int, int, int, int]


class OcrResourceError(RuntimeError):
    """OCR 资源缺失、损坏或清单不兼容。"""


class OcrEngine:
    """线程安全的 ONNX OCR；DirectML 失败时只回退一次 CPU。"""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._load_lock = threading.Lock()
        self._predict_lock = threading.Lock()
        self._det_session = None
        self._rec_session = None
        self._manifest: dict | None = None
        self._characters: list[str] = []
        self._provider = ""
        self._fell_back = False
        self._warmed = False

    @property
    def provider(self) -> str:
        return self._provider

    def preload(self) -> None:
        self._ensure_loaded()
        with self._predict_lock:
            if self._warmed:
                return
            try:
                self._warmup_impl()
            except Exception as exc:
                self._fallback_cpu(exc)
                self._warmup_impl()
            self._warmed = True

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_manifest(self) -> dict:
        path = RUNTIME_OCR / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OcrResourceError(f"OCR 清单无法读取：{path}") from exc
        if manifest.get("schema_version") != 1:
            raise OcrResourceError("OCR 清单版本不受支持")
        self._validate_manifest(manifest)
        for name in ("det.onnx", "rec.onnx", "characters.txt"):
            file_path = RUNTIME_OCR / name
            metadata = manifest["files"][name]
            expected = metadata["sha256"]
            if not expected or not file_path.is_file():
                raise OcrResourceError(f"OCR 资源缺失：{file_path}")
            if file_path.stat().st_size != metadata["size"]:
                raise OcrResourceError(f"OCR 资源大小不符：{file_path}")
            if self._sha256(file_path) != expected:
                raise OcrResourceError(f"OCR 资源校验失败：{file_path}")
        return manifest

    @staticmethod
    def _validate_manifest(manifest: dict) -> None:
        """在创建会话前拒绝不完整或与本实现不兼容的清单。"""
        try:
            files = manifest["files"]
            for name in ("det.onnx", "rec.onnx", "characters.txt"):
                metadata = files[name]
                digest = metadata["sha256"]
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError(f"{name} SHA256 无效")
                if int(metadata["size"]) <= 0:
                    raise ValueError(f"{name} 大小无效")

            det = manifest["det"]
            rec = manifest["rec"]
            for section in (det, rec):
                if not str(section["input_name"]).strip() or not str(section["output_name"]).strip():
                    raise ValueError("ONNX 输入输出名为空")

            resize = det["resize"]
            if str(resize["limit_type"]).lower() not in {"min", "max"}:
                raise ValueError("检测缩放类型无效")
            if int(resize["limit_side_len"]) <= 0 or int(resize["multiple"]) <= 0:
                raise ValueError("检测缩放尺寸无效")

            normalize = det["normalize"]
            if float(normalize["scale"]) <= 0:
                raise ValueError("检测归一化比例无效")
            if len(normalize["mean"]) != 3 or len(normalize["std"]) != 3:
                raise ValueError("检测归一化通道数无效")
            if any(float(value) == 0 for value in normalize["std"]):
                raise ValueError("检测归一化标准差无效")
            if str(normalize.get("channel_order", "bgr")).lower() not in {"bgr", "rgb"}:
                raise ValueError("检测颜色顺序无效")

            post = det["postprocess"]
            if not 0 <= float(post["thresh"]) <= 1:
                raise ValueError("检测二值化阈值无效")
            if not 0 <= float(post["box_thresh"]) <= 1:
                raise ValueError("检测框阈值无效")
            if int(post["max_candidates"]) <= 0 or int(post["min_size"]) <= 0:
                raise ValueError("检测候选数或最小尺寸无效")
            if float(post["unclip_ratio"]) <= 0:
                raise ValueError("检测扩框比例无效")

            image_shape = [int(value) for value in rec["image_shape"]]
            if len(image_shape) != 3 or image_shape[0] != 3 or min(image_shape) <= 0:
                raise ValueError("识别输入规格无效")
            if int(rec.get("max_width", image_shape[2])) < image_shape[2]:
                raise ValueError("识别最大宽度无效")
            if int(rec["batch_size"]) <= 0:
                raise ValueError("识别批量无效")
            if str(rec["decoder"]).lower() != "ctc" or int(rec["blank_index"]) != 0:
                raise ValueError("识别解码器不受支持")
        except (KeyError, TypeError, ValueError) as exc:
            raise OcrResourceError(f"OCR 清单字段无效：{exc}") from exc

    def _ensure_loaded(self) -> None:
        if self._det_session is not None:
            return
        with self._load_lock:
            if self._det_session is not None:
                return
            self._manifest = self._load_manifest()
            chars = (RUNTIME_OCR / "characters.txt").read_text(encoding="utf-8").splitlines()
            if " " not in chars:
                chars.append(" ")
            self._characters = [""] + chars
            requested = str(self._cfg.get("ocr_provider", "auto")).lower()
            try:
                self._create_sessions(requested)
            except Exception as exc:
                if requested not in ("auto", "dml"):
                    raise
                self._fell_back = True
                self._det_session = None
                self._rec_session = None
                _log.error("DirectML OCR 初始化失败，回退 CPU：%s", exc)
                self._create_sessions("cpu")

    def _create_sessions(self, requested: str) -> None:
        import onnxruntime as ort

        available = ort.get_available_providers()
        use_dml = requested in ("auto", "dml") and "DmlExecutionProvider" in available
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # DirectML 要求关闭内存图；纯 CPU 时开启可复用中间张量、略快
        options.enable_mem_pattern = not use_dml
        if use_dml:
            providers = [
                ("DmlExecutionProvider", {"device_id": int(self._cfg.get("ocr_device_id", 0))}),
                "CPUExecutionProvider",
            ]
            self._provider = "DmlExecutionProvider"
        else:
            providers = ["CPUExecutionProvider"]
            self._provider = "CPUExecutionProvider"
            if requested == "dml":
                _log.warning("DirectML 不可用，OCR 回退 CPU；可用 Provider=%s", available)
        self._det_session = ort.InferenceSession(
            str(RUNTIME_OCR / "det.onnx"), sess_options=options, providers=providers
        )
        self._rec_session = ort.InferenceSession(
            str(RUNTIME_OCR / "rec.onnx"), sess_options=options, providers=providers
        )
        _log.info("ONNX OCR 初始化完成 provider=%s", self._provider)

    def _fallback_cpu(self, exc: Exception) -> None:
        if self._provider != "DmlExecutionProvider" or self._fell_back:
            raise exc
        self._fell_back = True
        _log.error("DirectML OCR 运行失败，永久回退 CPU：%s", exc)
        self._det_session = None
        self._rec_session = None
        self._warmed = False
        self._create_sessions("cpu")

    def _warmup_impl(self) -> None:
        """在后台真正执行检测和识别图，避免第一次用户操作触发编译。"""
        det = self._manifest["det"]
        rec = self._manifest["rec"]
        det_side = max(32, int(det["resize"]["limit_side_len"]))
        det_multiple = max(1, int(det["resize"].get("multiple", 32)))
        det_side = int(math.ceil(det_side / det_multiple) * det_multiple)
        det_input = np.zeros((1, 3, det_side, det_side), dtype=np.float32)
        self._det_session.run(
            [det["output_name"]],
            {det["input_name"]: det_input},
        )
        channels, height, width = [int(value) for value in rec["image_shape"]]
        rec_input = np.zeros((1, channels, height, width), dtype=np.float32)
        self._rec_session.run(
            [rec["output_name"]],
            {rec["input_name"]: rec_input},
        )
        _log.info("ONNX OCR 空转完成 provider=%s", self._provider)

    def _prepare_image(self, image: np.ndarray) -> tuple[np.ndarray, float]:
        import cv2

        max_side = int(self._cfg.get("ocr_max_side") or 0)
        if max_side <= 0 or image is None or image.size == 0:
            return image, 1.0
        h, w = image.shape[:2]
        if max(h, w) <= max_side:
            return image, 1.0
        scale = max_side / float(max(h, w))
        out = cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        return out, 1.0 / scale

    def _det_input(self, image: np.ndarray) -> np.ndarray:
        import cv2

        cfg = self._manifest["det"]
        h, w = image.shape[:2]
        limit = int(cfg["resize"]["limit_side_len"])
        limit_type = str(cfg["resize"].get("limit_type", "min")).lower()
        if limit_type == "min":
            ratio = limit / min(h, w) if min(h, w) < limit else 1.0
        elif limit_type == "max":
            ratio = limit / max(h, w) if max(h, w) > limit else 1.0
        else:
            raise OcrResourceError(f"OCR 检测缩放类型不受支持：{limit_type}")
        multiple = max(1, int(cfg["resize"].get("multiple", 32)))
        nh = max(multiple, int(round(h * ratio / multiple) * multiple))
        nw = max(multiple, int(round(w * ratio / multiple) * multiple))
        resized = cv2.resize(image, (nw, nh))
        norm = cfg["normalize"]
        value = resized.astype(np.float32) * float(norm["scale"])
        if str(norm.get("channel_order", "bgr")).lower() == "rgb":
            value = value[:, :, ::-1]
        value = (value - np.asarray(norm["mean"], np.float32)) / np.asarray(norm["std"], np.float32)
        return np.transpose(value, (2, 0, 1))[None, ...].astype(np.float32)

    @staticmethod
    def _mini_box(contour: np.ndarray) -> tuple[np.ndarray, float]:
        import cv2

        rect = cv2.minAreaRect(contour)
        points = sorted(cv2.boxPoints(rect).tolist(), key=lambda point: point[0])
        left = sorted(points[:2], key=lambda point: point[1])
        right = sorted(points[2:], key=lambda point: point[1])
        return np.asarray([left[0], right[0], right[1], left[1]], np.float32), min(rect[1])

    @staticmethod
    def _box_score(pred: np.ndarray, box: np.ndarray) -> float:
        import cv2

        h, w = pred.shape
        x1 = max(0, min(int(np.floor(box[:, 0].min())), w - 1))
        x2 = max(0, min(int(np.ceil(box[:, 0].max())), w - 1))
        y1 = max(0, min(int(np.floor(box[:, 1].min())), h - 1))
        y2 = max(0, min(int(np.ceil(box[:, 1].max())), h - 1))
        mask = np.zeros((y2 - y1 + 1, x2 - x1 + 1), np.uint8)
        local = box.copy()
        local[:, 0] -= x1
        local[:, 1] -= y1
        cv2.fillPoly(mask, [local.astype(np.int32)], 1)
        return float(cv2.mean(pred[y1 : y2 + 1, x1 : x2 + 1], mask)[0])

    @staticmethod
    def _unclip(box: np.ndarray, ratio: float) -> np.ndarray | None:
        import cv2
        import pyclipper

        length = cv2.arcLength(box, True)
        if length <= 0:
            return None
        distance = cv2.contourArea(box) * ratio / length
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(box.astype(np.int64), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        expanded = offset.Execute(distance)
        if len(expanded) != 1:
            return None
        return np.asarray(expanded[0], np.float32).reshape(-1, 1, 2)

    def _detect(self, image: np.ndarray) -> list[np.ndarray]:
        import cv2

        inp = self._det_input(image)
        names = self._manifest["det"]
        pred = self._det_session.run([names["output_name"]], {names["input_name"]: inp})[0][0, 0]
        post = names["postprocess"]
        bitmap = (pred > float(post["thresh"])).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        h, w = image.shape[:2]
        ph, pw = pred.shape
        for contour in contours[: int(post["max_candidates"])]:
            box, short = self._mini_box(contour)
            if short < float(post["min_size"]):
                continue
            if self._box_score(pred, box) < float(post["box_thresh"]):
                continue
            expanded = self._unclip(box, float(post["unclip_ratio"]))
            if expanded is None:
                continue
            box, short = self._mini_box(expanded)
            if short < float(post["min_size"]) + 2:
                continue
            box[:, 0] = np.clip(np.round(box[:, 0] * w / pw), 0, w)
            box[:, 1] = np.clip(np.round(box[:, 1] * h / ph), 0, h)
            boxes.append(box)
        return boxes

    @staticmethod
    def _crop(image: np.ndarray, box: np.ndarray) -> np.ndarray:
        import cv2

        width = max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))
        height = max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))
        tw, th = max(1, int(width)), max(1, int(height))
        target = np.asarray([[0, 0], [tw, 0], [tw, th], [0, th]], np.float32)
        matrix = cv2.getPerspectiveTransform(box.astype(np.float32), target)
        crop = cv2.warpPerspective(image, matrix, (tw, th), borderMode=cv2.BORDER_REPLICATE)
        if crop.shape[0] / max(crop.shape[1], 1) >= 1.5:
            crop = np.rot90(crop)
        return np.ascontiguousarray(crop)

    @staticmethod
    def _sort_boxes(boxes: list[np.ndarray]) -> list[np.ndarray]:
        """复现 PaddleOCR 的阅读顺序：同一行优先按 x 排列。"""
        ordered = sorted(boxes, key=lambda box: (float(box[0, 1]), float(box[0, 0])))
        for index in range(len(ordered) - 1):
            cursor = index
            while cursor >= 0:
                current, following = ordered[cursor], ordered[cursor + 1]
                if abs(float(following[0, 1] - current[0, 1])) < 10 and following[0, 0] < current[0, 0]:
                    ordered[cursor], ordered[cursor + 1] = following, current
                    cursor -= 1
                else:
                    break
        return ordered

    @staticmethod
    def _rec_groups(ratios: list[float], *, target: int, spread: float) -> list[list[int]]:
        """把 OCR 行按宽高比分桶：同批比例接近可减少 padding，桶容量摊薄单次调用开销。"""
        order = sorted(range(len(ratios)), key=lambda i: ratios[i])
        groups: list[list[int]] = []
        current: list[int] = []
        for index in order:
            ratio = max(ratios[index], 1e-6)
            if current and (
                len(current) >= target
                or ratio / max(ratios[current[0]], 1e-6) > spread
            ):
                groups.append(current)
                current = []
            current.append(index)
        if current:
            groups.append(current)
        return groups

    def _rec_batch(self, crops: list[np.ndarray]) -> np.ndarray:
        import cv2

        channels, height, base_width = [
            int(value) for value in self._manifest["rec"]["image_shape"]
        ]
        if channels != 3:
            raise OcrResourceError(f"OCR 识别通道数不受支持：{channels}")
        max_width = int(self._manifest["rec"].get("max_width", base_width * 10))
        ratios = [crop.shape[1] / max(crop.shape[0], 1) for crop in crops]
        width = min(
            max_width,
            max(base_width, int(math.ceil(height * max(ratios) / 32) * 32)),
        )
        batch = np.zeros((len(crops), channels, height, width), np.float32)
        for index, (crop, ratio) in enumerate(zip(crops, ratios)):
            resized_w = min(width, max(1, int(math.ceil(height * ratio))))
            value = cv2.resize(crop, (resized_w, height)).astype(np.float32) * (2.0 / 255.0) - 1.0
            batch[index, :, :, :resized_w] = np.transpose(value, (2, 0, 1))
        return batch

    def _decode(self, logits: np.ndarray) -> list[tuple[str, float]]:
        blank_index = int((self._manifest or {}).get("rec", {}).get("blank_index", 0))
        result = []
        for sample in logits:
            indexes = sample.argmax(axis=1)
            probs = sample.max(axis=1)
            chars, scores, previous = [], [], -1
            for index, score in zip(indexes.tolist(), probs.tolist()):
                if index != blank_index and index != previous and index < len(self._characters):
                    chars.append(self._characters[index])
                    scores.append(float(score))
                previous = index
            result.append(("".join(chars), float(np.mean(scores)) if scores else 0.0))
        return result

    def _recognize_impl(self, image: np.ndarray) -> list[OcrLine]:
        boxes = self._detect(image)
        boxes = self._sort_boxes(boxes)
        crops = [self._crop(image, box) for box in boxes]
        rec = self._manifest["rec"]
        # 按宽高比分桶：同批比例接近可减少 padding，桶容量保证请求次数少
        decoded_by_index: dict[int, tuple[str, float]] = {}
        for chunk in self._rec_groups(
            [crop.shape[1] / max(crop.shape[0], 1) for crop in crops],
            target=max(int(rec.get("batch_size", 8)), _REC_TARGET_BATCH),
            spread=_REC_MAX_RATIO_SPREAD,
        ):
            inp = self._rec_batch([crops[i] for i in chunk])
            logits = self._rec_session.run([rec["output_name"]], {rec["input_name"]: inp})[0]
            for i, item in zip(chunk, self._decode(logits)):
                decoded_by_index[i] = item
        decoded = [decoded_by_index[i] for i in range(len(crops))]
        minimum = float(self._cfg.get("ocr_score_min") or 0.0)
        lines = []
        for box, (text, score) in zip(boxes, decoded):
            if not text.strip() or score < minimum:
                continue
            x1, y1 = box.min(axis=0).astype(int)
            x2, y2 = box.max(axis=0).astype(int)
            lines.append(OcrLine(text, score, (int(x1), int(y1), int(x2), int(y2))))
        return lines

    def recognize(self, image: np.ndarray) -> list[OcrLine]:
        self._ensure_loaded()
        if image is None or image.size == 0:
            return []
        work, inv = self._prepare_image(image)
        with self._predict_lock:
            try:
                lines = self._recognize_impl(work)
            except Exception as exc:
                self._fallback_cpu(exc)
                lines = self._recognize_impl(work)
        if inv == 1.0:
            return lines
        return [OcrLine(line.text, line.score, tuple(int(value * inv) for value in line.box)) for line in lines]

    @staticmethod
    def lines_to_text(lines: list[OcrLine]) -> str:
        return "\n".join(line.text for line in lines)
