# choir-solfa

A local tool that turns a song (link or file) into a four-part (SATB) tonic solfa score: PDF, text and MIDI. See README.md for what it does and how.

## Environment
- Use `.venv/bin/python` (native arm64 Python 3.11, made with uv). The system Python is x86 under Rosetta; don't use it.
- ffmpeg is at /usr/local/bin/ffmpeg. torchaudio is not installed, so demucs audio I/O is avoided: decode with ffmpeg via `audio.decode`.
- `setuptools<81` is pinned because resampy (used by basic-pitch) still imports pkg_resources.
- Basic Pitch prints debug lines; they are silenced with redirect_stdout in `audio.polyphonic_notes`.

## Commands
- Tests: `.venv/bin/python -m pytest -q`
- Page: `.venv/bin/choir-solfa serve --no-open --port 4790`. Set `CHOIR_SOLFA_HOME` to keep test jobs out of `~/.choir-solfa`.
- One song: `.venv/bin/choir-solfa make SONG -o out/ [--mode auto|choir|solo] [--time auto|4/4|3/4|2/4|6/8]`

## Conventions
- Grid cells: 4 per pulse in simple time, 2 per pulse in 6/8 (`models.METERS`). Pitches are sounding MIDI; `theory.written_pitch` raises tenor and bass by an octave for the page.
- Movable doh. Minor keys use the lah mode, so doh is the relative major.
- The server binds to 127.0.0.1 only and checks the Host header, and non-GET requests need the per-run X-Token. Keep both if you change routes.
- Messages shown on the page are plain words for choir leaders, not technical terms.

## Testing on real audio
Accuracy can only be judged by ear or against a known score. Silent Night SATB on YouTube (CZmC4qqL_Rg) is a useful check:
- It has a piano intro and then voices only.
- It should come out in 3/4 with doh B♭, and the soprano should open "s :-.l :s | m :- :-".

## Spotify
- Spotify audio is DRM-protected; yt-dlp refuses it. `fetch.get_song` reads title, artist and length from the public track page (og:title, music:musician_description, music:duration) and finds the same recording on YouTube (`find_on_youtube`, within 8 s).
- Fetch the track page with the plain `choir-solfa/0.1` user agent: a browser user agent gets a page without those meta tags.
- `Result.found_on` / `found_url` name the YouTube video used; the page shows them.
