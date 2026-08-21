# Silero TTS Server

HTTP server wrapping [silero-models](https://github.com/snakers4/silero-models)
for text-to-speech synthesis. Built with FastAPI, exposed on port 8768.
The caller (`bin/botctl-say`) converts the returned WAV to OGG Opus and
delivers it to Telegram.

## Available voices

| Voice   | Gender  | Use-case                                |
|---------|---------|-----------------------------------------|
| eugene  | male    | General Russian TTS, default            |
| aidar   | male    | Alternative male voice                  |
| baya    | female  | Female TTS                              |
| kseniya | neutral | Neutral tone                            |
| xenia   | female  | Alternative female voice                |

The operator assigns a voice per user in `state.json`; the server simply
honours the `voice` parameter.

## SSML support

Silero supports a subset of SSML tags:

```xml
<speak>
  <s>First sentence.</s>
  <s>Second sentence with a <break time="500ms"/> pause.</s>
  <prosody rate="fast">Spoken quickly.</prosody>
</speak>
```

Tags: `<speak>` (root), `<break>` (pause), `<prosody>` (rate/pitch),
`<s>` (sentence boundary).

## Russian accent marks

Use `+` before the stressed vowel for correct Russian pronunciation:

- `Прив+ет` — stress on the second syllable
- `м+ожно` — stress on the first syllable
- `д+елать` — stress on the first syllable

Without accent marks, Silero may place stress incorrectly, especially on
homographs and uncommon words.

## Build and run

```bash
docker build -t claude-tg-silero .
docker run --rm -p 127.0.0.1:8768:8768 claude-tg-silero
```

The build pre-warms the model cache, so the first container start is fast.

## Smoke test

```bash
curl -fs http://127.0.0.1:8768/health | jq
curl -X POST http://127.0.0.1:8768/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Прив+ет, м+ир","voice":"eugene"}' \
  --output /tmp/test.wav
```
