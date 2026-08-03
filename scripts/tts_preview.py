#!/usr/bin/env python3
"""Synthesise one sample with Volcano and write it to disk, to judge by ear.

Voice, speech rate, sample rate, emotion and the context prompt are only
settled by listening, so this drives the real provider. Every parameter
defaults to exactly what the pipeline ships; a flag overrides one of them.
One run makes at most one billed API call -- sweep by running it again.

Reads ``VOLCANO_API_KEY`` and ``VOLCANO_APP_ID`` -- both required for a real
call, since seed-tts-2.0 rejects a request without the App Id header -- so it
needs no AWS credentials: ``VolcanoProvider`` accepts credentials directly,
bypassing Secrets Manager.

Delivery tuning (speech rate, emotion and scale, context prompt) is passed to
the provider as an explicit ``VolcanoTuning``, so the real payload builder and
stream parser are exercised with nothing monkey-patched.

    export VOLCANO_API_KEY=...
    python scripts/tts_preview.py --speech-rate=-40
    python scripts/tts_preview.py --emotion neutral --emotion-scale 2
    python scripts/tts_preview.py --dry-run          # payload only, no spend

Speech rates are negative, so that flag needs ``--speech-rate=-40``: argparse
reads a leading dash in a separate argument as another option.

Being a debug tool, it defaults to loud: the full request (key masked), the
response status and every stream line (audio compressed to its size) are
printed, along with provider and urllib3 logging. The production provider
stays silent; the quiet is for the Lambda, not for this script.

Each call is billed per character, so keep the sample short.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import urllib3

from shared.tts import volcano
from shared.tts.base import VoiceConfig

# Short enough to keep a run cheap, long enough to hear pacing and the pause
# a paragraph break is supposed to produce.
SAMPLE_TEXT = """
Let your shoulders soften, and let the breath arrive on its own.

There is nothing here that needs solving. Whatever you carried in can wait
outside this moment.

Notice the weight of your body, held completely by what is beneath you.
"""


def parse_args() -> argparse.Namespace:
    # Defaults are None so an untouched flag is distinguishable from one set
    # to the pipeline value: only overridden axes name the output file.
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--voice",
        help=f"Speaker id (pipeline default: {volcano.DEFAULT_VOICE.voice_id}).",
    )
    parser.add_argument(
        "--speech-rate",
        type=int,
        help="Negative is slower. Use the = form: --speech-rate=-40 "
        f"(pipeline default: {volcano.DEFAULT_SPEECH_RATE}).",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        help=f"Hz (pipeline default: {volcano.DEFAULT_VOICE.sample_rate_hz}).",
    )
    parser.add_argument(
        "--emotion",
        help="Omitted by default, in the pipeline too -- the vendor's own delivery.",
    )
    parser.add_argument(
        "--emotion-scale",
        type=int,
        help=f"Only together with --emotion (default {volcano.DEFAULT_EMOTION_SCALE}).",
    )
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


def resolve_settings(args: argparse.Namespace) -> tuple[dict, list[str]]:
    """Pipeline defaults with the flags laid over them, plus what was overridden.

    Emotion and scale default to omitted, in the pipeline too; the pair
    reaches the request only when asked for.
    """
    overrides = {
        "voice": args.voice,
        "rate": args.speech_rate,
        "sr": args.sample_rate,
        "emotion": args.emotion,
        "scale": args.emotion_scale,
    }
    defaults = {
        "voice": volcano.DEFAULT_VOICE.voice_id,
        "rate": volcano.DEFAULT_SPEECH_RATE,
        "sr": volcano.DEFAULT_VOICE.sample_rate_hz,
        "emotion": volcano.DEFAULT_EMOTION,
        # None = not asked for; the tuning falls back to the pipeline's scale
        # if an emotion is set without one.
        "scale": None,
    }
    settings = {
        name: default if overrides[name] is None else overrides[name]
        for name, default in defaults.items()
    }
    overridden = [name for name, value in overrides.items() if value is not None]
    return settings, overridden


def label_for(settings: dict, overridden: list[str]) -> str:
    if not overridden:
        return "sample"
    # Voice ids are long; the trailing segment is the distinguishing part.
    parts = [
        f"{name}-{str(settings[name]).split('_')[-1] if name == 'voice' else settings[name]}"
        for name in overridden
    ]
    return "_".join(parts).replace(".", "-")


def _summarize_line(line: str) -> str:
    """One response line, with base64 audio compressed to its size."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return f"(unparseable) {line}"
    data = message.get("data")
    if data:
        message["data"] = f"<{len(data)} base64 chars>"
    return json.dumps(message, ensure_ascii=False)


class VerboseHTTP:
    """A PoolManager wrapper that prints each exchange.

    A debug script should show everything that crossed the wire; the provider
    deliberately shows nothing (constraint 7 is written for the Lambda). The
    access key is masked -- the rest is sample text and vendor responses,
    neither of which is secret here.
    """

    def __init__(self) -> None:
        self._http = urllib3.PoolManager()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        print(f"\n>>> {method} {url}")
        for name, value in kwargs.get("headers", {}).items():
            shown = "***" if name == "X-Api-Access-Key" else value
            print(f">>> {name}: {shown}")
        # The embedded ``additions`` JSON string prints escaped: that is the
        # wire format, shown as sent.
        print(json.dumps(json.loads(kwargs["body"]), indent=2, ensure_ascii=False))

        response = self._http.request(method, url, **kwargs)

        print(f"<<< HTTP {response.status}")
        for line in response.data.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                print(f"<<< {_summarize_line(line.strip())}")
        return response


def main() -> None:
    args = parse_args()
    # A debug script defaults to loud: provider INFO and urllib3's connection
    # chatter both go to stderr.
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

    # --dry-run never sends the request, and the credentials only ever reach
    # the request headers -- so the payload can be inspected without them.
    # Both variables are required for a real call: seed-tts-2.0 rejects a
    # request without the App Id header (HTTP 400, code 45000000), despite the
    # vendor doc calling it optional.
    api_key = os.environ.get("VOLCANO_API_KEY")
    app_id = os.environ.get("VOLCANO_APP_ID")
    if not args.dry_run and not (api_key and app_id):
        missing = [
            name
            for name, value in (("VOLCANO_API_KEY", api_key), ("VOLCANO_APP_ID", app_id))
            if not value
        ]
        sys.exit(f"{' and '.join(missing)} not set. Export before running (never commit them).")

    text = args.text_file.read_text(encoding="utf-8") if args.text_file else SAMPLE_TEXT

    # An explicit empty --context means "no delivery direction", which is itself
    # worth hearing; only a missing flag falls back to the shipped default.
    context_texts = (
        volcano.DEFAULT_CONTEXT_TEXTS if args.context is None else [c for c in args.context if c]
    )

    settings, overridden = resolve_settings(args)

    if args.emotion_scale is not None and args.emotion is None:
        sys.exit("--emotion-scale only applies together with --emotion.")

    # Emotion None makes the builder omit both emotion keys; --emotion
    # without --emotion-scale keeps the pipeline's scale.
    tuning = volcano.VolcanoTuning(
        speech_rate=settings["rate"],
        emotion=settings["emotion"],
        emotion_scale=(
            settings["scale"] if settings["scale"] is not None else volcano.DEFAULT_EMOTION_SCALE
        ),
        context_texts=context_texts,
    )

    provider = volcano.VolcanoProvider(
        http=VerboseHTTP(),
        credentials=volcano.VolcanoCredentials(
            # The placeholder cannot leak: dry-run returns before any request.
            api_key=api_key or "dry-run",
            app_id=app_id,
        ),
        tuning=tuning,
    )
    voice = VoiceConfig(settings["voice"], "en-AU", sample_rate_hz=settings["sr"])
    print(f"settings: {settings}  (overridden: {', '.join(overridden) or 'none'})")
    print(f"cluster: {volcano.cluster_for(settings['voice'])}")

    if args.dry_run:
        # Reaching into the private builder on purpose: the point of --dry-run
        # is to see the exact payload the provider would send.
        payload = provider._build_payload(text, voice)
        print(f"additions is a {type(payload['req_params']['additions']).__name__}")
        print(payload["req_params"]["additions"])
        return

    args.out.mkdir(parents=True, exist_ok=True)
    # Resolved so the line is copy-pastable into a player regardless of the
    # working directory the script was launched from.
    path = (args.out / f"{label_for(settings, overridden)}.mp3").resolve()
    print(f"{len(text)} characters -> {path}")

    audio = provider.synthesize(text, voice)
    path.write_bytes(audio)
    print(f"  {len(audio):>8,} bytes  {path}")


if __name__ == "__main__":
    main()
