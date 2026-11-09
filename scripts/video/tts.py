"""Render narration.json to one WAV per scene with Kokoro (local, open-weight), filling the factual placeholders.

    python scripts/video/tts.py --out build/video --facts facts.json
facts.json: {"LIVE_OR_SIM": ..., "FUND_NOTE": ..., "PAYOUT_NOTE": ..., "EVAL_CAPTION": ..., "EVAL_NARRATION": ...}
Writes <out>/audio/<scene>.wav and <out>/narration_durations.json (seconds, final narration and caption text).
"""
import argparse
import json
import os
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

HERE = Path(__file__).resolve().parent
MODELS = Path(os.environ.get("KOKORO_MODELS", "models/kokoro"))  # kokoro-v1.0.onnx + voices-v1.0.bin


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--facts", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    (out / "audio").mkdir(parents=True, exist_ok=True)
    spec = json.loads((HERE / "narration.json").read_text())
    facts = json.loads(Path(a.facts).read_text())
    k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    durations = {}
    for sid, sc in spec["scenes"].items():
        text, cap = sc["narration"], sc["caption"]
        for key, val in facts.items():
            text, cap = text.replace("{%s}" % key, val), cap.replace("{%s}" % key, val)
        assert "{" not in text and "{" not in cap, (sid, text, cap)
        samples, sr = k.create(" ".join(text.split()), voice=spec["voice"], speed=spec["speed"], lang="en-us")
        sf.write(out / "audio" / f"{sid}.wav", samples, sr)
        durations[sid] = {"seconds": round(len(samples) / sr, 2), "narration": " ".join(text.split()), "caption": cap}
        print(f"{sid}: {durations[sid]['seconds']}s")
    (out / "narration_durations.json").write_text(json.dumps(durations, indent=1))
    print("total narration", round(sum(v["seconds"] for v in durations.values()), 1), "s")


if __name__ == "__main__":
    main()
