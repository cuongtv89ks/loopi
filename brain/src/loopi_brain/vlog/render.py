"""The vlog renderer (spec §10.3, D15; P3 design §4.4; ruling R2): a prototype verified on 2026-10-17 on the brain
image's ffmpeg 7.1.5 (240 real 1080p 2 fps segments + 10 stills: 152 s wall, 212 s CPU, peak 198 MB), transcribed.

Pipeline (every ffmpeg call is built by a pure function returning its argv):
  0. inputs are checked in pure Python — MP4 frame/keyframe counts (moov parse), Ogg Opus duration, JPEG decode
     (Pillow); a bad input is skipped here, before any ffmpeg runs.
  1. base track: ONE ffmpeg over a concat-demuxer list of the day's segments, keeping exactly N = T*24 frames (uniform
     select), split into chunks at the stills' positions (forced keyframes + the segment muxer). Without segments: a
     keyframe slideshow (Pillow -> rawvideo pipe), 0.25 s per JPEG, split the same way.
  2. Ken Burns stills: Pillow -> one 1920x1080 rawvideo frame on stdin -> pre-scale x4 -> zoompan, D s at 24 fps.
  3. assembly: concat demuxer over [chunk, still, chunk, still, ...] with -c:v copy, one AAC track (silence + clips cut
     from the normalised Opus copies, faded, delayed to their stills) and a mov_text track from an SRT the code writes;
     -movflags +faststart. A WebVTT file with the same cues is written in Python (browsers do not show mov_text).

Every part is encoded with the same libx264 settings and output tags, so their avcC (SPS/PPS), timebase and SAR are
byte-identical, which is what makes `-c:v copy` concatenation valid. Success never depends on ffmpeg's stderr: each
step checks the exit code and then re-reads its output with the pure-Python MP4 parser (frame count, track kinds).
The stderr tail is kept on RenderError for debugging but is never put in its message: errors are stored and shown.
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

# --------------------------------------------------------------------------------------------- constants

FPS = 24
OUT_W, OUT_H = 1280, 720
KB_SRC_W, KB_SRC_H = 1920, 1080  # canvas Pillow hands to zoompan
KB_PRESCALE = 4  # 1920x1080 canvas x4 = 7680x4320 = 6x the output width: zoompan jitter ~0.2 px/frame
#                  (measured: no pre-scale 1.3 px, 2560w 1.0 px, 3840w 0.7 px, 5120w 0.6 px, 7680w 0.17 px)
SLIDE_SECONDS = 0.25
AUDIO_FADE = 0.3
AUDIO_CLIP_MAX = 8.0
AUDIO_CLIP_MIN = 1.0  # shorter remainders are dropped rather than played as a blip
# Keyframe-only decoding (-skip_frame nokey) picks the nearest keyframe instead of the exact frame, so each
# output step is off by up to one GOP: relative speed jitter <= N/keyframes. Require >= 3 keyframes per output
# frame (<= 33 % jitter); measured with GOP 3 s on a 4 h day (3.3 keys/frame): steps 9-12 s instead of 10 s.
KEYFRAMES_PER_OUTPUT_MIN = 3

FFMPEG = "ffmpeg"
BASE = [FFMPEG, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]

# Identical encoder settings + output tags for EVERY part => identical avcC => -c:v copy concat is valid.
X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-threads", "2",
    "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
    "-video_track_timescale", str(FPS * 512),  # 12288, same timescale in every part
]
# Fit inside 1280x720 keeping aspect, pad to exactly 1280x720, square pixels, limited range BT.709 4:2:0.
NORMALISE = (
    f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    f":out_range=tv:out_color_matrix=bt709,"
    f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
)
# Slideshow only: clone the last frame so it is held its full 0.25 s whatever the fps filter does at EOF
# (older ffmpeg emits 1 frame for it); `-frames:v N` stops the infinite clone. NOT used for the timelapse:
# there it would hide a truncated day (see timelapse_argv).
PAD_TAIL = "tpad=stop_mode=clone:stop=-1"


class RenderError(RuntimeError):
    """An ffmpeg step failed, or its output is not what it must be. The message names only the step and the exit
    code (it is stored and shown); ``stderr_tail`` keeps ffmpeg's last words for debugging, never logged."""

    def __init__(self, step: str, returncode: int | None, stderr_tail: str) -> None:
        super().__init__(f"{step}: ffmpeg exited {returncode}")
        self.step, self.returncode, self.stderr_tail = step, returncode, stderr_tail


class InvalidMedia(ValueError):
    """An input file that cannot be used (its message names the file, never its content)."""


# --------------------------------------------------------------------------------------------- MP4 parsing

@dataclass(frozen=True)
class Track:
    handler: str  # "vide", "soun", "sbtl"/"text"
    timescale: int
    duration: int  # in timescale units (mdhd)
    samples: int  # stsz sample_count = frames for video
    sync_samples: int  # stss entry_count (== samples when there is no stss box)

    @property
    def seconds(self) -> float:
        return self.duration / self.timescale if self.timescale else 0.0


def _boxes(f, start: int, end: int) -> Iterable[tuple[bytes, int, int]]:
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise InvalidMedia(f"truncated box {kind!r} at {pos}")
        yield kind, pos + hdr, pos + size
        pos += size


def _child(f, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for k, s, e in _boxes(f, start, end):
        if k == kind:
            return s, e
    return None


def probe_mp4(path: Path) -> list[Track]:
    """Tracks of an MP4 from its moov box only (no decoding). Raises InvalidMedia when moov is missing
    (e.g. a recording cut before the moov was written) or any top-level box overruns the file."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            moov = None
            for kind, s, e in _boxes(f, 0, size):
                if kind == b"moov":
                    moov = (s, e)
            if moov is None:
                raise InvalidMedia(f"{path}: no moov box")
            tracks = []
            for kind, s, e in _boxes(f, *moov):
                if kind != b"trak":
                    continue
                mdia = _child(f, s, e, b"mdia")
                if mdia is None:
                    continue
                hdlr, mdhd = _child(f, *mdia, b"hdlr"), _child(f, *mdia, b"mdhd")
                minf = _child(f, *mdia, b"minf")
                stbl = _child(f, *minf, b"stbl") if minf else None
                if not (hdlr and mdhd and stbl):
                    raise InvalidMedia(f"{path}: incomplete trak")
                f.seek(hdlr[0] + 8)
                handler = f.read(4).decode("latin-1")
                f.seek(mdhd[0])
                version = f.read(1)[0]
                if version == 1:
                    f.seek(mdhd[0] + 20)
                    timescale, duration = struct.unpack(">IQ", f.read(12))
                else:
                    f.seek(mdhd[0] + 12)
                    timescale, duration = struct.unpack(">II", f.read(8))
                stsz, stss = _child(f, *stbl, b"stsz"), _child(f, *stbl, b"stss")
                if stsz is None:
                    raise InvalidMedia(f"{path}: no stsz")
                f.seek(stsz[0] + 8)
                (samples,) = struct.unpack(">I", f.read(4))
                sync = samples
                if stss is not None:
                    f.seek(stss[0] + 4)
                    (sync,) = struct.unpack(">I", f.read(4))
                tracks.append(Track(handler, timescale, duration, samples, sync))
            return tracks
    except (OSError, struct.error) as exc:
        raise InvalidMedia(f"{path}: {exc}") from exc


def video_track(path: Path) -> Track:
    vids = [t for t in probe_mp4(path) if t.handler == "vide"]
    if len(vids) != 1 or vids[0].samples == 0:
        raise InvalidMedia(f"{path}: expected one non-empty video track")
    return vids[0]


# --------------------------------------------------------------------------------------------- Ogg Opus

def ogg_opus_duration(path: Path) -> float:
    """Playable seconds of an Ogg Opus file: (last granule - pre-skip) / 48000. Header reads only."""
    with open(path, "rb") as f:
        head = f.read(4096)
        if head[:4] != b"OggS":
            raise InvalidMedia(f"{path}: not Ogg")
        try:
            body = 27 + head[26]
            if head[body:body + 8] != b"OpusHead":
                raise InvalidMedia(f"{path}: not Opus")
            pre_skip = struct.unpack("<H", head[body + 10:body + 12])[0]
        except (IndexError, struct.error) as exc:  # cut inside the header: skip the clip, not the vlog
            raise InvalidMedia(f"{path}: truncated Ogg header") from exc
        size = f.seek(0, 2)
        f.seek(max(0, size - 65536))
        tail = f.read()
    i = tail.rfind(b"OggS")
    if i < 0 or i + 14 > len(tail):
        raise InvalidMedia(f"{path}: no final Ogg page")
    (granule,) = struct.unpack("<q", tail[i + 6:i + 14])
    return max(0.0, (granule - pre_skip) / 48000.0)


# --------------------------------------------------------------------------------------------- JPEG

def jpeg_canvas(path: Path, w: int, h: int) -> bytes:
    """Decode a JPEG with Pillow (EXIF orientation, CMYK/grey/progressive, any size), fit it into a w x h
    black canvas and return rgb24 bytes. Raises InvalidMedia for truncated/unreadable files -- ffmpeg would
    exit 0 on a truncated JPEG and silently render a half-grey frame."""
    try:
        with Image.open(path) as im:
            im.draft("RGB", (w, h))  # DCT-domain downscale of huge JPEGs (never below w x h)
            im = ImageOps.exif_transpose(im).convert("RGB")
            return ImageOps.pad(im, (w, h), method=Image.Resampling.BICUBIC, color=(0, 0, 0)).tobytes()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidMedia(f"{path}: {exc}") from exc


# --------------------------------------------------------------------------------------------- text files

def concat_list(paths: Iterable[Path]) -> str:
    """concat-demuxer script; single quotes in paths are escaped as '\\''."""
    lines = ["ffconcat version 1.0"]
    for p in paths:
        q = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{q}'")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _ts(seconds: float, sep: str) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def srt_text(cues: Sequence[Cue]) -> str:
    return "".join(f"{i}\n{_ts(c.start, ',')} --> {_ts(c.end, ',')}\n{c.text}\n\n"
                   for i, c in enumerate(cues, 1))


def vtt_text(cues: Sequence[Cue]) -> str:
    def esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("-->", "--&gt;")
    body = "".join(f"{_ts(c.start, '.')} --> {_ts(c.end, '.')}\n{esc(c.text)}\n\n" for c in cues)
    return "WEBVTT\n\n" + body


# --------------------------------------------------------------------------------------------- argv builders

def uniform_select(n_out: int, n_in: int) -> str:
    """select expression keeping exactly n_out of n_in frames, evenly spread: frame k kept iff
    (k*n_out mod n_in) < n_out. Quoted so the commas survive filtergraph parsing."""
    return f"select='lt(mod(n*{n_out},{n_in}),{n_out})'"


def _split_args(out_pattern: Path, boundaries: Sequence[int]) -> list[str]:
    """Output args for one file, or for chunks split exactly at the given output-frame numbers."""
    if not boundaries:
        return [str(out_pattern).replace("%03d", "000")]
    frames = ",".join(str(b) for b in boundaries)
    keys = "+".join(f"eq(n,{b})" for b in boundaries)
    return ["-force_key_frames", f"expr:{keys}", "-f", "segment", "-segment_format", "mp4",
            "-segment_frames", frames, "-reset_timestamps", "1", str(out_pattern)]


def timelapse_argv(list_path: Path, out_pattern: Path, *, n_out: int, n_in: int, keyframes_only: bool,
                   boundaries: Sequence[int] = (), decode_threads: int = 2) -> list[str]:
    """One ffmpeg over the concat list. n_in = frames (or keyframes when keyframes_only) in the list.
    setpts=N/(24*TB) repacks the kept frames at 1/24 s; `fps=24` then only sets the link frame rate (no
    dup/drop because timestamps already sit on the grid). Do NOT use `-r 24` instead: frames keep their 0.5 s
    duration and ffmpeg's cfr logic duplicated 11 of 144 frames and dropped the end of the day (measured).
    No tpad here: the concat demuxer treats a file it cannot open as END OF INPUT with exit code 0, so a short
    output must stay short and be caught by the frame-count check, not padded over."""
    vf = f"{uniform_select(n_out, n_in)},setpts=N/({FPS}*TB),fps={FPS},{NORMALISE}"
    return [*BASE, "-nostdin", "-threads", str(decode_threads),
            *(["-skip_frame", "nokey"] if keyframes_only else []),
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "passthrough",
            "-frames:v", str(n_out), *X264, *_split_args(out_pattern, boundaries)]


def slideshow_argv(out_pattern: Path, *, n_images: int, boundaries: Sequence[int] = ()) -> list[str]:
    """rgb24 1280x720 frames on stdin, one per JPEG, each held SLIDE_SECONDS at 24 fps."""
    per = round(SLIDE_SECONDS * FPS)
    vf = (f"setpts=N/({1 / SLIDE_SECONDS:g}*TB),scale=out_range=tv:out_color_matrix=bt709,format=yuv420p,"
          f"fps={FPS},setsar=1,{PAD_TAIL}")
    return [*BASE, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}",
            "-framerate", f"{1 / SLIDE_SECONDS:g}", "-i", "pipe:0",
            "-vf", vf, "-fps_mode", "passthrough", "-frames:v", str(per * n_images),
            *X264, *_split_args(out_pattern, boundaries)]


def kenburns_argv(out: Path, *, seconds: float = 3.0, zoom_from: float = 1.0, zoom_to: float = 1.1) -> list[str]:
    """One rgb24 KB_SRC_W x KB_SRC_H frame on stdin -> `seconds` of centred zoom at 24 fps.
    zoompan: d = output frames PER INPUT FRAME (input is exactly one frame), fps defaults to 25 (set it),
    zoom is driven by the output frame number `on` so it is linear and ends exactly at zoom_to."""
    nf = round(seconds * FPS)
    big_w, big_h = KB_SRC_W * KB_PRESCALE, KB_SRC_H * KB_PRESCALE
    z = f"{zoom_from}+({zoom_to}-{zoom_from})*on/{max(nf - 1, 1)}"
    vf = (f"scale={big_w}:{big_h}:flags=bicubic:out_range=tv:out_color_matrix=bt709,format=yuv420p,"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={nf}:s={OUT_W}x{OUT_H}:fps={FPS},"
          f"setsar=1,format=yuv420p")
    return [*BASE, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{KB_SRC_W}x{KB_SRC_H}", "-framerate", str(FPS),
            "-i", "pipe:0", "-vf", vf, "-frames:v", str(nf), *X264, str(out)]


@dataclass(frozen=True)
class ClipCut:
    ogg: Path
    offset: float  # seconds into the Opus copy
    length: float  # already clamped to what the file holds / the next clip / the video end
    start: float  # seconds into the vlog


def audio_graph(total: float, clips: Sequence[ClipCut], first_input: int) -> str:
    """Silence of `total` s with each clip faded in/out (0.3 s) and delayed to its start; mono 48 kHz.
    amix normalize=0: amix otherwise divides every input by the input count (clips get quieter)."""
    parts = [f"anullsrc=r=48000:cl=mono:d={total:.3f}[base]"]
    labels = ["[base]"]
    for i, c in enumerate(clips):
        fo = max(c.length - AUDIO_FADE, 0.0)
        parts.append(
            f"[{first_input + i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=mono,"
            f"afade=t=in:st=0:d={AUDIO_FADE},afade=t=out:st={fo:.3f}:d={AUDIO_FADE},"
            f"adelay=delays={round(c.start * 1000)}:all=1[c{i}]")
        labels.append(f"[c{i}]")
    parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
                 f"aformat=sample_rates=48000:channel_layouts=mono[aout]")
    return ";".join(parts)


def assemble_argv(parts_list: Path, out: Path, *, total: float, clips: Sequence[ClipCut], srt: Path,
                  sub_language: str = "vie") -> list[str]:
    clip_inputs: list[str] = []
    for c in clips:
        clip_inputs += ["-ss", f"{c.offset:.3f}", "-t", f"{c.length:.3f}", "-i", str(c.ogg)]
    sub_index = 1 + len(clips)
    return [*BASE, "-nostdin",
            "-f", "concat", "-safe", "0", "-i", str(parts_list),
            *clip_inputs,
            "-i", str(srt),
            "-filter_complex", audio_graph(total, clips, first_input=1),
            "-map", "0:v:0", "-map", "[aout]", "-map", f"{sub_index}:s:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "1",
            "-c:s", "mov_text", f"-metadata:s:s:0", f"language={sub_language}",
            "-video_track_timescale", str(FPS * 512), "-movflags", "+faststart", str(out)]


# --------------------------------------------------------------------------------------------- running

Feed = AsyncIterator[bytes] | None
Runner = Callable[[str, list[str], Feed], Awaitable[None]]


async def run_step(step: str, argv: list[str], feed: Feed = None, *, timeout: float = 3600.0) -> None:
    """Run one ffmpeg. Success = exit code 0 (stderr is only kept, last 8 KiB, for the error message).
    Kills the process on timeout or cancellation so no ffmpeg outlives the job."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if feed else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    tail = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(65536):
            tail.extend(chunk)
            del tail[:-8192]

    async def pump() -> None:
        assert proc.stdin is not None
        try:
            async for chunk in feed:  # type: ignore[union-attr]
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg died; its exit code tells why
        finally:
            proc.stdin.close()

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(drain_stderr(), *([pump()] if feed else []), proc.wait())
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise RenderError(step, proc.returncode, tail.decode("utf-8", "replace"))


# --------------------------------------------------------------------------------------------- planning

@dataclass(frozen=True)
class Segment:
    path: Path
    start: datetime  # wall-clock start of the recording


@dataclass(frozen=True)
class Still:
    jpeg: Path
    at: datetime
    caption: str
    clip_ogg: Path | None = None
    clip_offset: float = 0.0


@dataclass(frozen=True)
class Keyframe:
    jpeg: Path
    at: datetime


@dataclass
class RenderResult:
    out: Path
    vtt: Path
    frames: int
    seconds: float
    mode: str
    skipped: list[str] = field(default_factory=list)


def chunk_boundaries(cum_units_before: Sequence[int], n_in: int, n_out: int) -> list[int]:
    """Output-frame index where each still goes: number of selected frames among the first m input frames
    is ceil(m * n_out / n_in) for the uniform select above."""
    return [min(n_out, -(-m * n_out // n_in)) for m in cum_units_before]


async def _bytes_once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _slides(jpegs: Sequence[Path]) -> AsyncIterator[bytes]:
    for p in jpegs:
        yield await asyncio.to_thread(jpeg_canvas, p, OUT_W, OUT_H)


def _inspect_segments(segments: Sequence[Segment]) -> tuple[list[tuple[Segment, Track]], list[str]]:
    """The readable segments with their video track, in time order, and why each other one is skipped."""
    good: list[tuple[Segment, Track]] = []
    skipped: list[str] = []
    for s in sorted(segments, key=lambda s: s.start):
        try:
            good.append((s, video_track(s.path)))
        except InvalidMedia as exc:
            skipped.append(f"segment {exc}")
    return good, skipped


def _write_concat(dest: Path, paths: Sequence[Path]) -> None:
    dest.write_text(concat_list(paths), encoding="utf-8")


async def render(*, segments: Sequence[Segment], stills: Sequence[Still], keyframes: Sequence[Keyframe],
                 target_seconds: float, workdir: Path, out: Path, still_seconds: float = 3.0,
                 title: str | None = None, allow_keyframes_only: bool = True,
                 runner: Runner = run_step) -> RenderResult:
    workdir.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []
    n_out = round(target_seconds * FPS)

    # ---- 0. inspect inputs (pure Python; bad inputs are skipped, never handed to ffmpeg). The segment reads (up to
    # ~1000 a day, a cold disk) run in a thread: they must not stall the worker's loop and its heartbeat.
    good, bad_segments = await asyncio.to_thread(_inspect_segments, segments)
    skipped.extend(bad_segments)
    ok_stills: list[tuple[Still, None]] = []
    for st in sorted(stills, key=lambda s: s.at):
        try:
            await asyncio.to_thread(jpeg_canvas, st.jpeg, 16, 9)  # full decode check, tiny output
            ok_stills.append((st, None))
        except InvalidMedia as exc:
            skipped.append(f"still {exc}")

    # ---- 1. base track, split at the stills' positions
    chunk_pat = workdir / "chunk_%03d.mp4"
    if good:
        frames = sum(t.samples for _, t in good)
        keys = sum(t.sync_samples for _, t in good)
        keyframes_only = allow_keyframes_only and keys >= KEYFRAMES_PER_OUTPUT_MIN * n_out
        units = [(s, t.sync_samples if keyframes_only else t.samples, t.seconds) for s, t in good]
        n_in = keys if keyframes_only else frames
        n_out = min(n_out, n_in)

        def units_before(at: datetime) -> int:
            m = 0
            for s, u, secs in units:
                into = (at - s.start).total_seconds()
                if into >= secs:
                    m += u
                elif into > 0:
                    m += math.ceil(u * into / secs)
            return m

        bounds = chunk_boundaries([units_before(st.at) for st, _ in ok_stills], n_in, n_out)
        lst = workdir / "segments.ffconcat"
        await asyncio.to_thread(_write_concat, lst, [s.path for s, _ in good])
        cut = sorted({b for b in bounds if 0 < b < n_out})
        await runner("timelapse", timelapse_argv(lst, chunk_pat, n_out=n_out, n_in=n_in,
                                                 keyframes_only=keyframes_only, boundaries=cut), None)
        mode = "timelapse-keyframes" if keyframes_only else "timelapse-full"
    else:
        ok_kf: list[Keyframe] = []
        for k in sorted(keyframes, key=lambda k: k.at):
            try:
                await asyncio.to_thread(jpeg_canvas, k.jpeg, 16, 9)  # decode check
                ok_kf.append(k)
            except InvalidMedia as exc:
                skipped.append(f"keyframe {exc}")
        if not ok_kf and not ok_stills:
            raise InvalidMedia("nothing to render")
        per = round(SLIDE_SECONDS * FPS)
        n_out = per * len(ok_kf)
        bounds = [per * sum(1 for k in ok_kf if k.at < st.at) for st, _ in ok_stills]
        cut = sorted({b for b in bounds if 0 < b < n_out})
        if ok_kf:
            await runner("slideshow", slideshow_argv(chunk_pat, n_images=len(ok_kf), boundaries=cut),
                         _slides([k.jpeg for k in ok_kf]))
        mode = "slideshow"
    chunk_edges = [0, *cut, n_out] if n_out else [0]
    chunks = [Path(str(chunk_pat).replace("%03d", f"{i:03d}")) for i in range(len(chunk_edges) - 1)]
    for c, (a, b) in zip(chunks, zip(chunk_edges, chunk_edges[1:])):
        try:
            got = video_track(c).samples
        except InvalidMedia:
            got = 0
        if got != b - a:  # e.g. a segment vanished/corrupted after inspection: concat stops there, rc 0
            raise RenderError(mode, 0, f"{c.name}: {got} frames, expected {b - a}")

    # ---- 2. Ken Burns stills
    kb_frames = round(still_seconds * FPS)
    still_parts: list[Path] = []
    for i, (st, _) in enumerate(ok_stills):
        p = workdir / f"still_{i:03d}.mp4"
        canvas = await asyncio.to_thread(jpeg_canvas, st.jpeg, KB_SRC_W, KB_SRC_H)
        await runner(f"kenburns[{i}]", kenburns_argv(p, seconds=still_seconds), _bytes_once(canvas))
        if video_track(p).samples != kb_frames:
            raise RenderError(f"kenburns[{i}]", 0, f"{p.name}: wrong frame count")
        still_parts.append(p)

    # ---- 3. interleave, cues, audio clips, assemble
    order: list[tuple[Path, int, Still | None]] = []
    for ci, c in enumerate(chunks):
        if ci == 0:
            order += [(still_parts[j], kb_frames, ok_stills[j][0]) for j, b in enumerate(bounds) if b == 0]
        order.append((c, chunk_edges[ci + 1] - chunk_edges[ci], None))
        order += [(still_parts[j], kb_frames, ok_stills[j][0])
                  for j, b in enumerate(bounds) if b == chunk_edges[ci + 1] and b > 0]
    if not chunks:
        order = [(still_parts[j], kb_frames, ok_stills[j][0]) for j in range(len(ok_stills))]
    total_frames = sum(n for _, n, _ in order)
    total = total_frames / FPS

    cues: list[Cue] = [Cue(0.0, min(3.0, total), title)] if title else []
    starts: list[tuple[Still, float]] = []
    pos = 0
    for _, n, st in order:
        if st is not None:
            starts.append((st, pos / FPS))
            cues.append(Cue(pos / FPS, (pos + n) / FPS, st.caption))
        pos += n
    if title:  # mov_text cannot show overlapping cues: end the title where the first caption starts
        first = min((c.start for c in cues[1:]), default=total)
        cues[0] = Cue(0.0, min(3.0, total, first) if first > 0 else min(3.0, total), title)
        if first == 0:
            cues.pop(0)
    cues.sort(key=lambda c: c.start)

    clip_cuts: list[ClipCut] = []
    with_clip = [(st, t0) for st, t0 in starts if st.clip_ogg is not None][:3]
    for i, (st, t0) in enumerate(with_clip):
        assert st.clip_ogg is not None
        try:
            avail = ogg_opus_duration(st.clip_ogg) - st.clip_offset
        except (InvalidMedia, OSError) as exc:
            skipped.append(f"clip {exc}")
            continue
        nxt = with_clip[i + 1][1] if i + 1 < len(with_clip) else total
        length = min(AUDIO_CLIP_MAX, avail, nxt - t0, total - t0)
        if length < AUDIO_CLIP_MIN:
            skipped.append(f"clip {st.clip_ogg.name}@{st.clip_offset}: only {max(avail, 0):.2f}s available")
            continue
        clip_cuts.append(ClipCut(st.clip_ogg, st.clip_offset, round(length, 3), t0))

    srt = workdir / "subs.srt"
    srt.write_text(srt_text(cues), encoding="utf-8")
    vtt = out.with_suffix(".vtt")
    vtt.write_text(vtt_text(cues), encoding="utf-8")
    parts = workdir / "parts.ffconcat"
    parts.write_text(concat_list(p for p, _, _ in order), encoding="utf-8")
    await runner("assemble", assemble_argv(parts, out, total=total, clips=clip_cuts, srt=srt), None)

    tracks = probe_mp4(out)
    kinds = sorted(t.handler for t in tracks)
    vid = [t for t in tracks if t.handler == "vide"]
    if kinds != ["sbtl", "soun", "vide"] or vid[0].samples != total_frames:
        raise RenderError("assemble", 0, f"unexpected output tracks {kinds} / frames {vid and vid[0].samples}")
    return RenderResult(out, vtt, total_frames, total, mode, skipped)
