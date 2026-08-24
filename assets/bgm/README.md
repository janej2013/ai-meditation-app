# Background music

The PWA mixes one of these under the narration in the browser (README:
"Mixing happens in the browser"). `frontend/src/audio/mixer.ts` lists the
tracks offered to the listener; the first is the default and starts on the
home screen the moment a session begins.

## Licensing — tracks are deployed, not committed

The real tracks are licensed from [Pixabay](https://pixabay.com/service/terms/)
under its Content License: commercial use, no attribution required, but **no
standalone redistribution of the file itself**. A public repository is exactly
that, so the tracks and their licence certificates are gitignored and uploaded
to the audio bucket by hand:

```bash
make upload-bgm ENV=dev            # assets/bgm/default_bgm.mp3
make upload-bgm ENV=dev BGM_TRACKS="assets/bgm/a.mp3 assets/bgm/b.mp3"
```

`BucketDeployment` (pipeline stack) syncs only what git holds and never prunes,
so a later deploy leaves the uploads alone. Keep each track's Pixabay licence
certificate beside it locally (`*license*.txt`, also gitignored) as the record
of the grant.

| file | source | in git |
|---|---|---|
| `default_bgm.mp3` | Pixabay #322801, "Meditation – Meditation Music" by ikoliks_aj | no — `make upload-bgm` |
| `silence.mp3` | generated: three seconds of silent MPEG frames | yes |

## Preparing a track

The mixer fetches the whole file and decodes it to PCM in memory, so length is
what costs: a 10-minute stereo file decodes to ~200 MB, which a phone will not
keep. Cut a loopable two-to-three-minute section, mono, and fade both ends so
the loop seam is inaudible:

```bash
ffmpeg -i source.mp3 -t 180 -ac 1 -ar 44100 -b:a 96k \
       -af "afade=t=in:d=2,afade=t=out:st=178:d=2" assets/bgm/default_bgm.mp3
```

## `silence.mp3`

Not a listening option. It ships with the repository because CI's post-deploy
smoke fetches it through the audio distribution to prove the CORS headers a
real browser needs are present (`.github/workflows/ci.yml`).
