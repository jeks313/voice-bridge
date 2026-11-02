"""Speech in (Whisper on the local GPU) and speech out (Drama 3 on khelt, plus pacing)."""

import asyncio
import io
import os
import re
import subprocess
import wave

import aiohttp
import numpy as np

GLOSSARY = [g.strip() for g in open(os.environ.get("GLOSSARY", "/app/glossary.txt")) if g.strip()]
TTS_URL = os.environ.get("TTS_URL", "http://100.119.99.59:8091")
VOICE = os.environ.get("TTS_VOICE", "seed8")
TEMPO = os.environ.get("TTS_TEMPO", "1.08")
PAUSE_CAP_MS = int(os.environ.get("TTS_PAUSE_CAP_MS", "250"))
# How names are said; applied to the text sent to TTS only.
SAY = {r"\bk3s\b": "k three s", r"\btlg1\b": "T L G one", r"\bkhelt\b": "kelt", r"\bherdr\b": "herder",
       r"\bJSONL\b": "JSON L", r"\bWSL\b": "W S L", r"\bk8s\b": "kubernetes", r"\bPWA\b": "P W A",
       r"\bTTS\b": "T T S", r"\bSTT\b": "S T T", r"\bLAN\b": "lan"}


def _preload_cuda_libs():
    """pip-installed CUDA libs (nvidia-cublas-cu12, nvidia-cudnn-cu12) aren't on the loader path by default."""
    import ctypes, sys
    for sp in sys.path:
        for lib in ("nvidia/cublas/lib/libcublas.so.12", "nvidia/cudnn/lib/libcudnn.so.9"):
            p = os.path.join(sp, lib)
            if os.path.exists(p):
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)


class STT:
    def __init__(self):
        _preload_cuda_libs()
        from faster_whisper import WhisperModel
        self.model = WhisperModel(os.environ.get("WHISPER_MODEL", "/models/whisper-turbo"),
                                  device=os.environ.get("WHISPER_DEVICE", "cuda"),
                                  compute_type=os.environ.get("WHISPER_COMPUTE", "int8"))
        self.hotwords = " ".join(GLOSSARY)
        self.lock = asyncio.Lock()

    @staticmethod
    def decode(data: bytes) -> np.ndarray:
        out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
                              "-f", "s16le", "pipe:1"], input=data, capture_output=True, check=True).stdout
        return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768

    def _run(self, audio):
        if audio.size == 0 or np.abs(audio).max() < 0.003:
            return ""
        segs, _ = self.model.transcribe(audio, language="en", beam_size=5, hotwords=self.hotwords,
                                        vad_filter=False, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segs).strip()

    async def transcribe(self, data: bytes) -> str:
        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, self.decode, data)
        async with self.lock:
            return await loop.run_in_executor(None, self._run, audio)


def speakable(text: str) -> str:
    name = lambda p: re.sub(r"\.\w{1,4}$", "", p.rstrip("/").split("/")[-1])  # a path is said as its file name
    t = re.sub(r"`([^`]*)`", lambda m: name(m.group(1)) if "/" in m.group(1) else m.group(1), text)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"<?https?://[^\s>)]+>?", "the link", t)  # a bare URL is tappable on screen, never read out
    # a path: starts with ~ / ./, or has two slashes, or ends in a file extension (not 24/7, and/or)
    t = re.sub(r"(?<![\w/])(?:(?:~|\.{1,2})?/(?:[\w.-]+/)*[\w.-]+|[\w.-]+/[\w.-]+/[\w./-]+|[\w.-]+/[\w-]+\.\w{1,4})(?![\w/])",
               lambda m: name(m.group(0)), t)
    t = re.sub(r"[*_#>|~]", "", t)
    for pat, say in SAY.items():
        t = re.sub(pat, say, t, flags=re.I)
    return " ".join(t.split())


class SentenceSplitter:
    """Feed text deltas; get back complete sentences as soon as they end."""

    END = re.compile(r"(.+?[.!?](?:[\"')\]]+)?)(\s+|$)", re.S)

    CLAUSE = re.compile(r"(.{30,}?[,;:—])(?:\s+|$)", re.S)  # first chunk may break at a clause

    def __init__(self):
        self.buf = ""
        self.emitted = False

    def feed(self, delta: str):
        self.buf += delta
        out = []
        if not self.emitted:
            m, e = self.CLAUSE.match(self.buf), self.END.match(self.buf)
            if m and len(m.group(1)) <= 90 and (not e or m.end(1) < e.end(1)):
                out.append(m.group(1).strip())
                self.buf = self.buf[m.end():]
        while True:
            if "\n" in self.buf and not self.END.match(self.buf.split("\n", 1)[0] + " "):
                head, self.buf = self.buf.split("\n", 1)
                if head.strip():
                    out.append(head.strip())
                continue
            m = self.END.match(self.buf)
            if not m or m.end() == len(self.buf) and not m.group(2):
                break  # sentence not finished yet (no space after the stop)
            out.append(m.group(1).strip())
            self.buf = self.buf[m.end():]
        out = [s for s in out if s]
        self.emitted = self.emitted or bool(out)
        return out

    def flush(self):
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def cap_pauses(pcm: np.ndarray, sr: int, cap_ms: int) -> np.ndarray:
    fr = int(0.01 * sr)
    n = len(pcm) // fr
    if n == 0:
        return pcm
    frames = pcm[: n * fr].reshape(n, fr).astype(np.float32)
    e = np.sqrt((frames ** 2).mean(axis=1))
    sil = e < 0.03 * max(e.max(), 1)
    keep = np.ones(n, bool)
    capf = cap_ms // 10
    i = 0
    while i < n:
        if sil[i]:
            j = i
            while j < n and sil[j]:
                j += 1
            if i > 0 and j < n and j - i > capf:
                mid = i + capf // 2
                keep[mid: mid + (j - i - capf)] = False
            i = j
        else:
            i += 1
    return np.concatenate([frames[keep].reshape(-1), pcm[n * fr:].astype(np.float32)]).astype(np.int16)


def pace(wav_bytes: bytes) -> bytes:
    w = wave.open(io.BytesIO(wav_bytes))
    sr = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    pcm = cap_pauses(pcm, sr, PAUSE_CAP_MS)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                          "-filter:a", f"atempo={TEMPO}", "-f", "s16le", "pipe:1"],
                         input=pcm.tobytes(), capture_output=True, check=True).stdout
    buf = io.BytesIO()  # wrap it ourselves: a piped ffmpeg WAV carries a placeholder length
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(raw)
    return buf.getvalue()


class TTS:
    def __init__(self):
        self.http = None

    async def _session(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
        return self.http

    async def warm(self):
        try:
            s = await self._session()
            async with s.post(f"{TTS_URL}/warm", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return (await r.json()).get("state")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return f"unreachable: {e.__class__.__name__}"

    async def status(self):
        try:
            s = await self._session()
            async with s.get(f"{TTS_URL}/status", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return (await r.json()).get("state")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return "unreachable"

    async def speak(self, text: str) -> bytes:
        s = await self._session()
        async with s.post(f"{TTS_URL}/v1/speak", json={"text": speakable(text), "voice": VOICE},
                          timeout=aiohttp.ClientTimeout(total=60, sock_connect=3)) as r:
            r.raise_for_status()
            raw = await r.read()
        return await asyncio.get_running_loop().run_in_executor(None, pace, raw)
