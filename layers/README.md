# Lambda layers

Both layers are **generated, not committed** — build them with
`scripts/build_layers.sh` before `cdk deploy`. `cdk synth` succeeds without
them but emits a warning naming the missing one.

## `shared/`

`python/` holds the `shared` package and its dependencies (pydantic, boto3),
installed from `backend/`. This is what lets the step Lambdas ship as small zip
functions while still importing `shared.db` — pydantic cannot go into a zip
without a build step, which is exactly what this script provides.

## `ffmpeg/`

`bin/` holds static `ffmpeg` and `ffprobe` binaries used by `mix_audio`.

**Source:** <https://johnvansickle.com/ffmpeg/> — the long-standing static
Linux build (`ffmpeg-release-amd64-static.tar.xz`), GPL-licensed. Override with
`FFMPEG_URL=... scripts/build_layers.sh ffmpeg` to pin a specific release or
use an internal mirror.

The binaries land at `/opt/bin/ffmpeg` and `/opt/bin/ffprobe` inside the Lambda;
`mix_audio` reads those paths from `FFMPEG_PATH` / `FFPROBE_PATH`.
