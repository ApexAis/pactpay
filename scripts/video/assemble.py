"""Assemble the recorded PactPay demo: cut pure-wait regions, add narration and captions, encode (NVENC if present).

    python scripts/video/assemble.py --out build/video [--ffmpeg PATH]

Inputs in <out>: timeline.json (record_demo.py), narration_durations.json and audio/*.wav (tts.py).
Output: <out>/pactpay_demo.mp4 (1920x1080, H.264 + AAC) and <out>/pactpay_demo.srt (captions as a file too).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def fmt_ass(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def fmt_srt(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def esc_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", default=None)
    a = ap.parse_args()
    out = Path(a.out)
    ff = a.ffmpeg
    if ff is None:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    tl = json.loads((out / "timeline.json").read_text())
    narr = json.loads((out / "narration_durations.json").read_text())
    off = tl["offset"]
    events = tl["events"]
    cuts = sorted([(e["start"] + off, e["end"] + off, e["real_seconds"], e["label"]) for e in events
                   if e.get("cut") and e["end"] - e["start"] > 0.5])
    scenes = [e for e in events if "scene" in e]
    end = max(e["end"] for e in scenes) + off + 0.5

    # keep intervals in source time
    keep, cur = [], 0.0
    for s, e, _, _ in cuts:
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    keep.append((cur, end))

    def mapped(t: float) -> float:
        """source video time -> output time"""
        acc = 0.0
        for s, e in keep:
            if t <= s:
                return acc
            if t <= e:
                return acc + (t - s)
            acc += e - s
        return acc

    # 1. constant-frame-rate copy of the raw recording, then the kept parts concatenated
    src = Path(tl["video"])
    cfr = out / "cfr.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-vf", "fps=30", "-c:v", "libx264", "-preset",
                    "veryfast", "-crf", "16", "-an", str(cfr)], check=True)
    parts = "".join(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];" for i, (s, e) in enumerate(keep))
    concat = "".join(f"[v{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=0[vc]"

    # 2. captions (ASS burned in, SRT alongside)
    ass = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "",
           "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
           "MarginR, MarginV, Encoding",
           "Style: Cap,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00301A0A,&HC0301A0A,1,0,0,0,100,100,0,0,3,18,0,1,60,60,52,1",
           "Style: Note,DejaVu Sans,30,&H0000C4FF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,3,12,0,9,40,40,120,1",
           "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    srt, n = [], 0
    audio_inputs, delays = [], []
    for sc in scenes:
        sid = sc["scene"]
        t0, t1 = mapped(sc["start"] + off), mapped(sc["end"] + off)
        cap = narr[sid]["caption"]
        ass.append(f"Dialogue: 0,{fmt_ass(t0)},{fmt_ass(t1)},Cap,,0,0,0,,{esc_ass(cap)}")
        n += 1
        srt += [str(n), f"{fmt_srt(t0)} --> {fmt_srt(t1)}", narr[sid]["narration"], ""]
        audio_inputs.append(out / "audio" / f"{sid}.wav")
        delays.append(t0 + 0.25)
    for s, _e, real, label in cuts:
        t = mapped(s)
        ass.append(f"Dialogue: 1,{fmt_ass(t - 0.2)},{fmt_ass(t + 2.6)},Note,,0,0,0,,"
                   f"⏩ {real:.0f} s of real waiting skipped ({esc_ass(label)})")
    (out / "captions.ass").write_text("\n".join(ass) + "\n")
    (out / "pactpay_demo.srt").write_text("\n".join(srt) + "\n")

    # 3. narration: each scene's audio at its output start time
    total = sum(e - s for s, e in keep)
    amix = "".join(f"[{i + 1}:a]adelay={int(d * 1000)}|{int(d * 1000)}[a{i}];" for i, d in enumerate(delays))
    amix += "".join(f"[a{i}]" for i in range(len(delays))) + f"amix=inputs={len(delays)}:normalize=0,apad,atrim=0:{total:.2f}[ac]"
    fc = parts + concat + f";[vc]ass={out / 'captions.ass'}[vo];" + amix
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    x264 = ["-c:v", "libx264", "-preset", "slow", "-crf", "20"]
    probe = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout

    def encode(enc: list[str]) -> subprocess.CompletedProcess:
        cmd = [ff, "-y", "-loglevel", "error", "-i", str(cfr)]
        for wav in audio_inputs:
            cmd += ["-i", str(wav)]
        cmd += ["-filter_complex", fc, "-map", "[vo]", "-map", "[ac]", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "192k", "-movflags", "+faststart", str(out / "pactpay_demo.mp4")]
        return subprocess.run(cmd, capture_output=True, text=True)

    r = encode(nvenc) if "h264_nvenc" in probe else None
    encoder = "h264_nvenc (RTX GPU)"
    if r is None or r.returncode != 0:
        r, encoder = encode(x264), "libx264"
    if r.returncode != 0:
        raise SystemExit(r.stderr[-3000:])
    print(f"encoder: {encoder}")
    print(f"wrote {out / 'pactpay_demo.mp4'}: {total:.1f} s ({len(cuts)} waits cut)")


if __name__ == "__main__":
    main()
