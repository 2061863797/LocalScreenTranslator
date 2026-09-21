# -*- coding: utf-8 -*-
"""llama-server 进程管理：启动、健康检查、关闭。

参数针对「本机短文本翻译」调优（小 ctx、单 slot、可选 KV 量化），
host 固定读配置（默认 127.0.0.1，仅本机）。
"""

import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import requests

from .applog import get_logger
from .paths import resolve_path

_log = get_logger("llama")


# 仅允许本机回环，防止误配 0.0.0.0 把翻译服务暴露到局域网
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def sanitize_server_host(host: str | None) -> str:
    """配置/启动时强制本机 host。"""
    h = (host or "127.0.0.1").strip().lower()
    if h not in _LOCAL_HOSTS:
        _log.warning("server_host=%r 非本机，已强制 127.0.0.1", host)
        return "127.0.0.1"
    # 统一成 IPv4 回环，避免 [::1] 与 127.0.0.1 混用
    if h in ("localhost", "::1"):
        return "127.0.0.1"
    return h


class LlamaServer:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        # 支持 config 里写 runtime/llama 这类相对项目根的路径
        self.llama_dir = resolve_path(cfg["llama_dir"])
        self.model_path = resolve_path(cfg["model_path"])
        self.host = sanitize_server_host(cfg.get("server_host"))
        cfg["server_host"] = self.host
        self.port = int(cfg["server_port"])
        self._proc: subprocess.Popen | None = None
        self._recent_output: deque[str] = deque(maxlen=30)
        self._output_thread: threading.Thread | None = None
        self._cuda_available: bool | None = None
        # 启动时预热与首次翻译可能并发调用 start，串行化避免重复拉起进程
        self._lock = threading.Lock()
        # stop() 先设置事件，再等待锁；这样可以中断持锁的冷启动循环。
        self._stop_requested = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def actual_device(self) -> str:
        configured = str(self._cfg.get("llama_device", "auto")).lower()
        if configured == "cpu" or self._cuda_available is False:
            return "CPU"
        if self._cuda_available is True:
            return "GPU (CUDA)"
        return configured.upper()

    def is_healthy(self, timeout: float = 2.0) -> bool:
        try:
            r = requests.get(f"{self.base_url}/health", timeout=timeout)
            if r.status_code != 200:
                return False
            data = r.json()
            return (
                isinstance(data, dict)
                and str(data.get("status", "")).strip().lower() in {"ok", "ready"}
            )
        except (requests.RequestException, ValueError):
            return False

    def check_model_match(self, timeout: float = 2.0) -> bool:
        """检查已有运行实例加载的模型是否与当前配置的模型匹配。"""
        model_name = self.model_path.name.casefold()
        model_stem = self.model_path.stem.casefold()
        try:
            # 1. 尝试从 /models 端点获取模型列表
            r = requests.get(f"{self.base_url}/models", timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    for item in data.get("data", []):
                        m_id = str(item.get("id", "")).casefold()
                        if model_name in m_id or model_stem in m_id:
                            return True
            # 2. 尝试从 /props 端点获取默认模型配置
            r = requests.get(f"{self.base_url}/props", timeout=timeout)
            if r.status_code == 200:
                props = r.json()
                if isinstance(props, dict):
                    m_str = str(props.get("default_generation_settings", {}).get("model", "")).casefold()
                    if model_name in m_str or model_stem in m_str:
                        return True
        except Exception:
            pass
        return False

    def _read_output(self, proc: subprocess.Popen) -> None:
        """持续消费子进程输出，既避免管道堵塞，也保留启动诊断。"""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in stream:
                line = str(raw).rstrip()
                if not line:
                    continue
                self._recent_output.append(line)
                _log.info("server: %s", line[:1000])
        except (OSError, ValueError):
            pass

    def _terminate_process_locked(self) -> None:
        """结束并回收本程序持有的进程；调用方必须持有 _lock。"""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self._proc = None
        if self._output_thread is not None:
            self._output_thread.join(timeout=0.5)
            self._output_thread = None

    def _has_cuda_device(self, exe: Path) -> bool:
        """询问当前 llama 包是否实际发现 CUDA 设备，结果在进程周期内缓存。"""
        if self._cuda_available is not None:
            return self._cuda_available
        try:
            probe = subprocess.run(
                [str(exe), "--list-devices"],
                cwd=str(self.llama_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            output = f"{probe.stdout}\n{probe.stderr}".upper()
            self._cuda_available = probe.returncode == 0 and "CUDA" in output
        except (OSError, subprocess.SubprocessError):
            self._cuda_available = False
        return self._cuda_available

    def _gpu_layers(self, exe: Path) -> int:
        device = str(self._cfg.get("llama_device", "auto")).strip().lower()
        if device == "cpu":
            return 0
        has_cuda = self._has_cuda_device(exe)
        if device == "gpu" and not has_cuda:
            raise RuntimeError("已选择 GPU，但当前 llama-server 未检测到可用的 CUDA 设备")
        if device == "auto" and not has_cuda:
            return 0
        requested = int(self._cfg.get("n_gpu_layers", 99))
        return requested if requested > 0 else 99

    def _build_cmd(self, exe: Path) -> list[str]:
        """按配置拼 llama-server 参数（翻译场景默认偏快、省显存）。"""
        cfg = self._cfg
        ngl = self._gpu_layers(exe)
        threads = int(cfg.get("threads", 8))
        ctx = int(cfg.get("ctx_size", 2048))
        batch = int(cfg.get("batch_size", 512))
        ubatch = int(cfg.get("ubatch_size", 256))
        slots = int(cfg.get("parallel_slots", 1))
        flash = bool(cfg.get("flash_attn", True))
        mlock = bool(cfg.get("mlock", False))
        ctk = str(cfg.get("cache_type_k") or "").strip()
        ctv = str(cfg.get("cache_type_v") or "").strip()

        # 合理夹紧，避免配错把服务拉挂
        ctx = max(512, min(ctx, 8192))
        batch = max(64, min(batch, 4096))
        ubatch = max(32, min(ubatch, batch))
        slots = max(1, min(slots, 4))
        threads = max(1, min(threads, 64))

        cmd = [
            str(exe),
            "-m", str(self.model_path),
            "-ngl", str(ngl),
            "-t", str(threads),
            "-c", str(ctx),
            "-b", str(batch),
            "-ub", str(ubatch),
            "-np", str(slots),
            "--flash-attn", "on" if flash else "auto",
            "--port", str(self.port),
            "--host", self.host,
        ]
        if mlock:
            cmd.append("--mlock")
        if ctk:
            cmd.extend(["-ctk", ctk])
        if ctv:
            cmd.extend(["-ctv", ctv])
        return cmd

    def start(self, wait_seconds: int = 180) -> None:
        """启动 llama-server 并等待就绪。已有健康实例则直接复用。

        线程安全：并发 start 会排队，后者等待前者完成后若已健康则直接返回。
        冷启动加载模型可能较久，默认最多等 180 秒。
        """
        with self._lock:
            if self._stop_requested.is_set():
                raise InterruptedError("llama-server 启动已取消")
            if self.is_healthy():
                # 若已有健康实例，严格校验加载的模型是否匹配，防止误复用其他模型或未知进程
                if self.check_model_match():
                    _log.info("复用已有健康且模型匹配的实例 %s", self.base_url)
                    return
                _log.warning("端口 %s 上的已有实例模型不匹配，不能复用", self.port)
            exe = self.llama_dir / "llama-server.exe"
            if not exe.exists():
                raise FileNotFoundError(f"未找到 llama-server：{exe}")
            if not self.model_path.exists():
                raise FileNotFoundError(f"未找到模型文件：{self.model_path}")

            # 上一轮进程还活着却不健康时必须先回收，不能覆盖引用变成孤儿进程。
            if self._proc is not None:
                if self._proc.poll() is None:
                    _log.warning("回收未通过健康检查的旧 llama-server pid=%s", self._proc.pid)
                self._terminate_process_locked()

            cmd = self._build_cmd(exe)
            _log.info(
                "启动 llama-server port=%s device=%s ngl=%s ctx=%s batch=%s/%s np=%s mlock=%s ctk=%s model=%s",
                self.port,
                self._cfg.get("llama_device", "auto"),
                cmd[cmd.index("-ngl") + 1],
                self._cfg.get("ctx_size"),
                self._cfg.get("batch_size"),
                self._cfg.get("ubatch_size"),
                self._cfg.get("parallel_slots"),
                self._cfg.get("mlock"),
                self._cfg.get("cache_type_k") or "-",
                self.model_path,
            )
            _log.info("cmdline: %s", " ".join(cmd))
            # CREATE_NO_WINDOW：后台运行不弹黑框
            self._recent_output.clear()
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.llama_dir),
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._output_thread = threading.Thread(
                target=self._read_output,
                args=(self._proc,),
                daemon=True,
                name="llama-output",
            )
            self._output_thread.start()
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if self._stop_requested.is_set():
                    _log.info("llama-server 启动被退出流程取消")
                    self._terminate_process_locked()
                    raise InterruptedError("llama-server 启动已取消")
                if self.is_healthy():
                    _log.info(
                        "llama-server 就绪 pid=%s base=%s",
                        self._proc.pid if self._proc else "?",
                        self.base_url,
                    )
                    return
                if self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._proc = None
                    if self._output_thread is not None:
                        self._output_thread.join(timeout=0.5)
                        self._output_thread = None
                    detail = " | ".join(list(self._recent_output)[-5:])
                    _log.error("llama-server 启动后立即退出 code=%s output=%s", code, detail)
                    raise RuntimeError(
                        f"llama-server 启动后立即退出（返回码 {code}），"
                        f"{detail or '没有可用的服务端输出'}"
                    )
                self._stop_requested.wait(0.5)
            _log.error("llama-server %s 秒内未就绪", wait_seconds)
            self._terminate_process_locked()
            raise TimeoutError(f"llama-server 在 {wait_seconds} 秒内未就绪")

    def stop(self) -> None:
        """仅关闭本程序拉起的进程，不影响用户自己启动的实例。"""
        self._stop_requested.set()
        with self._lock:
            if self._proc is not None:
                _log.info("停止本程序拉起的 llama-server pid=%s", self._proc.pid)
            self._terminate_process_locked()
