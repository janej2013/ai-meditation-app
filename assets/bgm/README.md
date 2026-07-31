# Background music

`BucketDeployment` syncs this directory to `s3://<audio-bucket>/assets/bgm/`,
and `mix_audio` reads the track named by the `BGM_KEY` environment variable.

## `silence.mp3` — placeholder

Three seconds of silent MPEG-1 Layer III frames, generated rather than
licensed. It exists so the pipeline runs end-to-end and the mix filter graph is
exercised; `-stream_loop -1` loops it under the whole narration, so the output
is simply the narration with an inaudible bed.

## Adding real tracks

Drop licensed royalty-free MP3s in here and point `BGM_KEY` at one:

```bash
cd infra && npx cdk deploy -c bgm_key=assets/bgm/<your-track>.mp3
```

Requirements: MP3, 44.1 kHz, and long enough that looping isn't obvious —
two minutes or more works well. Keep the licence file alongside each track.

**Do not commit anything you don't hold redistribution rights for.** This
repository is public-facing.
