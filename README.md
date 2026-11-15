# Choir Solfa

Give it a song (a link or an audio file). Get the tonic solfa for soprano, alto, tenor and bass as a printable PDF, plus a text version and a MIDI file to practise with.

- **A choir singing parts:** each part is transcribed as it was sung.
- **One lead singer:** the melody becomes the soprano line, and alto, tenor and bass are written as a hymn-style arrangement under it.

Everything runs on your own Mac. Nothing is uploaded anywhere except the download from the link you give it.

## Setup

You need [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`). Node.js is optional; it helps with some YouTube links.

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
```

On Apple Silicon, use a native arm64 Python 3.11. uv downloads one if needed. An x86 Python running under Rosetta is much slower and can't use the Core ML note model.

The first song you run downloads the Demucs voice-separation model (about 80 MB).

## Use it

The web page:

```bash
.venv/bin/choir-solfa
```

The page opens at http://127.0.0.1:4790. Paste a link (Spotify, YouTube and most sites yt-dlp supports) or drop an MP3, WAV, M4A or video file anywhere on the page. A 4-minute song takes about 2 to 4 minutes.

The page checks the link as you paste it. It tells you, for example, when a Spotify link is a playlist or album rather than one song. When a score is ready, "Doesn't look right?" offers one-click fixes: switch between choir and lead singer, or change the time signature. Each fix makes the score again from the same song.

### Spotify links

In Spotify, open the song and use **Share → Copy song link**. Album, playlist and artist links are refused with a message.

Spotify's audio is protected, so it can't be downloaded. Instead, the tool reads the song's title, artist and length from Spotify's public song page, then searches YouTube for the same recording. It takes the closest match within 8 seconds of the Spotify length, preferring the artist's official upload and avoiding karaoke, instrumental, cover and live versions. The page and the command line both say which YouTube video was used, so you can listen and check it's the right recording. If no close match exists, it says so: upload the file or paste a YouTube link instead.

From the command line:

```bash
.venv/bin/choir-solfa make "https://open.spotify.com/track/..." -o out/
.venv/bin/choir-solfa make "https://www.youtube.com/watch?v=..." -o out/
.venv/bin/choir-solfa make song.mp3 --mode solo --time 3/4 --title "Silent Night"
```

`--mode` is `auto`, `choir` or `solo`. `--time` is `auto`, `4/4`, `3/4`, `2/4` or `6/8`.

## How it works

1. **Get the audio:** yt-dlp for links (no playlists, at most 15 minutes and 300 MB). Spotify links are matched to the same recording on YouTube, as described above.
2. **Separate the voices:** Demucs (htdemucs) splits the voices from the instruments. If the song is mostly voices (a cappella), it listens to the whole mix instead, because separation tends to drop the lowest voices.
3. **Hear the notes:** Spotify's Basic Pitch finds every note sung, several at once. If three or more notes are often sounding together, the song is treated as a choir; otherwise as a solo.
4. **Find the beat and the bars:** librosa tracks the beat. A beat faster than 160 a minute is halved, because in echoing rooms the tracker can lock onto half beats. The beat is nudged to line up with where notes actually start. The time signature (3/4 or 4/4) is guessed from where long notes and chord changes fall.
5. **Find the key:** Krumhansl key profiles over the chroma and the notes heard. Minor keys are written in the lah mode, so doh is the relative major, as is usual in tonic solfa.
6. **Write the parts:**
   - Choir: notes are sorted into four parts by range, keeping each line smooth.
   - Solo: pyin follows the melody, then chords are chosen per beat and voiced with standard part-writing rules (no parallel fifths or octaves).
7. **Lay out the score:** standard Curwen notation (`d :r .m ,f`, `-` to hold, octave marks d¹ and d₁). Tenor and bass are written an octave above where they sound, as is customary.

## Limits

This is automatic transcription. Treat the result as a first draft and check every part by ear before teaching it.

- **Choir parts that cross or double:** they are hard to tell apart. The tool hears pitches, not who sang them.
- **Basses:** basses are often heard an octave high, or missed when the recording has little low end.
- **Held notes:** a held note sometimes shows as re-sung, especially when another part changes words at the same moment.
- **Time signature:** only 3/4 and 4/4 are guessed. Choose 6/8 or 2/4 yourself.
- **Key changes:** the song is written in a single key.
- **Spotify matches:** the YouTube copy is found by title, artist and length. It is usually the same recording, but listen to check.
- **Solo or choir:** a solo singer over a busy piano can be taken for a choir. Choose "One lead singer" if so.

## Development

```bash
.venv/bin/python -m pytest -q
```

The code is under `src/choir_solfa/`. `pipeline.py` runs the steps above, `server.py` and `web/index.html` are the page, and `pdf.py` and `notation.py` lay out the score.
