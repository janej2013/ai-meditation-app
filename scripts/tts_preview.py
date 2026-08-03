#!/usr/bin/env python3
"""Synthesise samples with Volcano and write them to disk, to judge by ear.

Voice, speech rate, sample rate, emotion and the context prompt are only
settled by listening, so this drives the real provider with those parameters
varied. It is a tuning tool, not part of the deployment.

Reads the API key from ``VOLCANO_API_KEY`` (and optionally ``VOLCANO_APP_ID``),
so it needs no AWS credentials -- ``VolcanoProvider`` accepts credentials
directly, bypassing Secrets Manager.

``speech_rate``, ``emotion``, ``emotion_scale`` and ``context_texts`` are module
constants baked into ``_build_payload``, so this patches them per combination.
That keeps the tuning surface out of production code while still exercising the
real payload builder and stream parser.

Every flag takes a comma-separated list; the run is the product of them all, one
file per combination, named after whichever axes actually vary.

    export VOLCANO_API_KEY=...
    python scripts/tts_preview.py --speech-rate=-40,-25,-10
    python scripts/tts_preview.py --emotion ASMR,neutral --emotion-scale 2,4
    python scripts/tts_preview.py --dry-run          # payload only, no spend

Speech rates are negative, so that flag needs ``--speech-rate=-40``: argparse
reads a leading dash in a separate argument as another option.

Each call is billed per character, so keep the sample short while sweeping.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
from pathlib import Path

from shared.tts import volcano
from shared.tts.base import VoiceConfig

# Short enough to keep a sweep cheap, long enough to hear pacing and the pause
# a paragraph break is supposed to produce.
SAMPLE_TEXT = """\
Let your shoulders soften, and let the breath arrive on its own.

There is nothing here that needs solving. Whatever you carried in can wait
outside this moment.

Notice the weight of your body, held completely by what is beneath you.
"""

MAX_COMBINATIONS = 24


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _ints(raw: str) -> list[int]:
    return [int(part) for part in _split(raw)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--voice", default=volcano.DEFAULT_VOICE.voice_id)
    parser.add_argument(
        "--speech-rate",
        default=str(volcano.DEFAULT_SPEECH_RATE),
        help="Negative is slower. Use the = form: --speech-rate=-40,-25",
    )
    parser.add_argument("--sample-rate", default=str(volcano.DEFAULT_VOICE.sample_rate_hz))
    parser.add_argument("--emotion", default=volcano.DEFAULT_EMOTION)
    parser.add_argument("--emotion-scale", default=str(volcano.DEFAULT_EMOTION_SCALE))
    parser.add_argument(
        "--context",
        action="append",
        help="Delivery direction. Repeat for several; omit to use the built-in default. "
        "Pass an empty string to synthesise with no direction at all.",
    )
    parser.add_argument("--text-file", type=Path, help="Read the sample from a file instead.")
    parser.add_argument("--out", type=Path, default=Path("tts-preview"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request payload and exit without calling the API.",
    )
    return parser.parse_args()


def build_combinations(args: argparse.Namespace) -> tuple[list[dict], list[str]]:
    """Cartesian product of every axis, plus the names of the ones that vary."""
    axes = {
        "voice": _split(args.voice),
        "rate": _ints(args.speech_rate),
        "sr": _ints(args.sample_rate),
        "emotion": _split(args.emotion),
        "scale": _ints(args.emotion_scale),
    }
    varying = [name for name, values in axes.items() if len(values) > 1]
    combos = [dict(zip(axes, values, strict=True)) for values in itertools.product(*axes.values())]
    return combos, varying


def label_for(combo: dict, varying: list[str]) -> str:
    if not varying:
        return "sample"
    # Voice ids are long; the trailing segment is the distinguishing part.
    parts = [
        f"{name}-{str(combo[name]).split('_')[-1] if name == 'voice' else combo[name]}"
        for name in varying
    ]
    return "_".join(parts).replace(".", "-")


def main() -> None:
    args = parse_args()

    api_key = os.environ.get("VOLCANO_API_KEY")
    if not api_key:
        sys.exit("VOLCANO_API_KEY is not set. Export it before running (never commit it).")

    text = args.text_file.read_text(encoding="utf-8") if args.text_file else SAMPLE_TEXT

    # An explicit empty --context means "no delivery direction", which is itself
    # worth hearing; only a missing flag falls back to the shipped default.
    context_texts = (
        volcano.DEFAULT_CONTEXT_TEXTS if args.context is None else [c for c in args.context if c]
    )

    combos, varying = build_combinations(args)
    if len(combos) > MAX_COMBINATIONS:
        sys.exit(f"{len(combos)} combinations exceeds the {MAX_COMBINATIONS} cap; narrow a flag.")

    provider = volcano.VolcanoProvider(
        credentials=volcano.VolcanoCredentials(
            api_key=api_key, app_id=os.environ.get("VOLCANO_APP_ID")
        )
    )

    if args.dry_run:
        combo = combos[0]
        volcano.DEFAULT_SPEECH_RATE = combo["rate"]
        volcano.DEFAULT_EMOTION = combo["emotion"]
        volcano.DEFAULT_EMOTION_SCALE = combo["scale"]
        volcano.DEFAULT_CONTEXT_TEXTS = context_texts
        voice = VoiceConfig(combo["voice"], "en-AU", sample_rate_hz=combo["sr"])
        # Reaching into the private builder on purpose: the point of --dry-run
        # is to see the exact payload the provider would send.
        payload = provider._build_payload(text, voice)
        print(f"cluster: {volcano.cluster_for(combo['voice'])}")
        print(f"additions is a {type(payload['req_params']['additions']).__name__}")
        print(payload["req_params"]["additions"])
        return

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(combos)} combination(s), {len(text)} characters each -> {args.out}/\n")

    for combo in combos:
        # Patched per combination; see the module docstring.
        volcano.DEFAULT_SPEECH_RATE = combo["rate"]
        volcano.DEFAULT_EMOTION = combo["emotion"]
        volcano.DEFAULT_EMOTION_SCALE = combo["scale"]
        volcano.DEFAULT_CONTEXT_TEXTS = context_texts

        voice = VoiceConfig(combo["voice"], "en-AU", sample_rate_hz=combo["sr"])
        path = args.out / f"{label_for(combo, varying)}.mp3"

        try:
            audio = provider.synthesize(text, voice)
        # Broad on purpose: one bad combination should not end the sweep.
        except Exception as exc:
            print(f"  FAILED  {path.name}: {type(exc).__name__}: {exc}")
            continue

        path.write_bytes(audio)
        print(f"  {len(audio):>8,} bytes  {path.name}")


if __name__ == "__main__":
    main()
