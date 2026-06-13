# NERVICE — VOICE SPEECH RESEARCH (pacing + phrasing for natural spoken output)

Research-only (2026-06-13). **No code, no installs, no config changes.** Feeds a future
persona+pacing batch. Question: *how should a voice assistant phrase and PACE its text so spoken
output sounds natural and unhurried on long explanations — not robotic or rushed?* Free/established
practice, honest tradeoffs, real sources linked. Reference to Nervice's current pipeline is by
function name only.

Context: Nervice synthesizes with **Kokoro** (voice `am_echo`, 24 kHz) over a sentence-pipeline (see
§5). Part 1 of this branch already added spoken-symbol normalization in `_clean_for_speech`, and the
Silero batch added a 90 ms inter-sentence pause in `_synth`. This doc plans the *next* layer.

---

## 1. Speaking rate / pacing

**Kokoro has native speed control.** Nervice already passes it: `k.create(text, voice, speed=1.0,
lang)`. Range is wide — roughly **0.1×–5.0×** (`<1.0` slower, `>1.0` faster). Kokoro's naturalness is
top-ranked at the default, and the universal TTS finding is **keep rate changes moderate** — large
deviations sound unnatural. Practically: ~**0.85–1.1×** is the safe band; below ~0.75× the speech
stretches into a droning, unnatural drawl (Kokoro speed is a *uniform time-stretch*, including within
words — it does **not** insert intelligent pauses, it just slows everything).

**Global slowdown vs length-variable pacing — the honest call:**
- **Global** (e.g. always `speed=0.9`): long explanations breathe better, but **short replies
  ("Four.", "Done.", "Yes.") feel dragged and sluggish** — actively bad for a terse assistant.
- **Length-variable** (normal for short, slightly slower for long): mirrors how people actually talk
  — nobody drawls "yes." A simple 2-tier rule (short reply → ~1.0×; long explanation past ~N
  sentences/chars → ~0.92×) captures most of the benefit with none of the sluggishness.
- **Recommendation:** length-variable, but keep the slowdown *small* and treat rate as the **second**
  lever, not the first. The dominant naturalness lever is **pauses/structure (§2)**, not raw rate —
  because Kokoro's speed knob stretches uniformly, whereas real silence between clauses is what the
  ear reads as "unhurried." Do pauses first; add a gentle length-variable rate only if still rushed.

## 2. Natural pauses (the biggest lever)

Across voice-over and TTS practice, **monotonous pacing is the #1 tell that audio is machine-made**,
and *pauses* — not speed — are what make narration sound human and let a listener absorb a long
answer. Two free mechanisms:

- **Punctuation-as-pause.** Kokoro (via its phonemizer) honors punctuation prosody: a comma is a
  slight pause, a period a sentence break, an em-dash a fine-grained beat. Part 1's normalization
  *already* converts structural symbols (`/ | • —`) into commas — so it doubles as a pacing aid, not
  just a de-robotizer. Writing in **shorter sentences with clause commas** is the cheapest "breathe"
  there is.
- **Inserted silence.** Nervice already adds a **90 ms trailing silence per sentence**
  (`INTER_SENTENCE_PAUSE_MS` in `_synth`). That's the "breathing" knob. It can be made
  **punctuation/structure-aware** — a longer gap after sentence-final `.`/`?`/`!` than after a comma,
  and a longer gap between topic shifts ("paragraphs") — which is a precise, deterministic, free
  improvement.

**SSML is the industry standard for this** (`<break>` for pauses — ~250 ms brief, ~500 ms sentence,
~1 s section; `<prosody rate=…>` to slow key points and speed transitions). **Kokoro does NOT accept
SSML**, so Nervice can't use the tags directly — but the *principles* transfer: emulate `<break>`
with real inserted silence (the pause knob) and `<prosody>` with the length-variable speed (§1).
Net: **pacing should come mostly from structure** — short sentences, clause commas, inter-sentence
and inter-topic silence — and only secondarily from the rate knob.

## 3. Text-normalization landscape (the deeper version of Part 1)

Part 1 handled *symbols* (`- / & % | •`). The deeper job — **numbers, currency, times, dates,
abbreviations, units, URLs → spoken form** — is the classic TTS preprocessing step called **Text
Normalization (TN)**.

**Honest baseline first:** Kokoro's phonemizer backend (espeak-ng, via misaki/phonemizer) *already*
does **some** TN — it expands many bare numbers and a few symbols on its own (this is why some
numbers/percent already speak acceptably). But espeak's TN is inconsistent, locale-quirky, and not
controllable, and **espeak-ng is GPL-3.0**. Don't rely on it for correctness; treat it as a partial
safety net.

**Free libraries, with licenses + honest fit for Nervice (Windows, pip/venv, RTX 4060):**

| Library | License | What it does | Fit for Nervice |
|---|---|---|---|
| **num2words** | **LGPL-2.1** | numbers → words: cardinal, ordinal, **currency**, year (50+ langs). *Numbers only — no dates/times/abbrev.* | ✅ **Best low-risk pick.** Pure Python, pip, Windows-trivial. LGPL is fine when imported unmodified. Covers the number/currency/ordinal piece cleanly. |
| **NeMo-text-processing** | **Apache-2.0** | production WFST TN: numbers, currency, **dates, times, measures**, etc. (NVIDIA; used in real ASR/TTS). | ⚠️ **High friction here.** Depends on **Pynini, which pip cannot build on Windows/macOS** — needs `conda install -c conda-forge pynini`. Powerful but heavy and awkward on a Windows venv box; not worth it for a single user. |
| **nltk** | **Apache-2.0** | not a TN engine; sentence/clause tokenization (`punkt`). | Useful for *segmentation* to drive pausing (§2), not for number/date expansion. Pip, Windows-fine. |
| hand-rolled regex (Part 1 style) | n/a (your code) | deterministic case-by-case | ✅ Free, Windows-trivial, zero deps, fully controllable — but you must enumerate cases; good for the common tail, gives up on the rare. |

**Recommendation:** keep the **deterministic regex** approach for common, safe cases (times
`3:30` → "three thirty", a short abbreviation map `Dr.`→"Doctor", `mph`→"miles per hour",
`°F`→"degrees"), and add **num2words** for the number/currency/ordinal piece (pip, LGPL,
Windows-safe). **Avoid NeMo/Pynini** unless the project moves to conda — the Windows build pain
outweighs the gain for one user. **Critical guard:** be conservative — version strings (`v4.2`),
IDs, and commit hashes (`a918e75`) must **not** be number-expanded; gate expansion so the spoken
stream never reads an identifier digit-by-digit.

## 4. Writing for the ear vs writing for the screen (and the terse-persona tension)

**Screen text is scanned** (bullets, symbols, hashes, fragments, tables); **ear text is linear** —
heard once, in order, with no scroll-back — so it must front-load the answer, use complete short
clauses, and drop visual-only constructs (paths, long IDs, code, tables).

**The tension:** Nervice's persona is deliberately terse ("the fewest well-chosen words… no
preamble, no padding"). Terse *screen* text reads **clipped and telegraphic aloud** — fragments like
"GPU 82%, 67°C, 6/8GB" sound robotic when voiced. **Terse ≠ telegraphic.**

**The resolution:** stay terse in *substance* (lead with the answer, zero filler) but phrase for the
*ear* — complete short clauses with natural connectives, screen-only tokens (hashes/paths/symbols)
dropped from the spoken stream, and let **pacing/pauses** carry the unhurried feel instead of adding
words. The persona already gestures at this ("short-spoken **but well-spoken**"); this just
operationalizes it. Concrete before → after (screen → spoken):

| Screen (as written) | Spoken (ear-phrased, still terse) |
|---|---|
| `GPU: 82%, 6.2/8 GB, 67°C.` | "GPU's busy — 82 percent, about six of your eight gigs, 67 degrees." |
| `Done. Committed a918e75.` | "Done — committed." *(hash is screen-only)* |
| `3 opts: A) X B) Y C) Z` | "Three options. First X, second Y, third Z." |
| `Weather: 54°F, wind 12mph NW, rain 30%.` | "Fifty-four degrees, light northwest wind, thirty percent chance of rain." |
| `See docs/PROJECT_STATE.md` | "It's in the project-state doc." *(don't spell a path aloud)* |

**Principle:** keep **two renderings** of every reply — the **screen transcript** (terse, exact,
with hashes/paths) and the **spoken stream** (ear-phrased). Nervice *already* has this split (HUD
text frames vs the `_clean_for_speech`'d audio), so the ear-phrasing layer is additive, not a
persona rewrite.

## 5. Where pacing + normalization slot into the existing pipeline (reference only — no code)

Current flow (app/voice.py + streaming.py): a reply is split into sentences; each sentence →
`_clean_for_speech` (markdown/URL strip + Part-1 symbol normalization) → `_synth` (Kokoro
`k.create(..., speed=1.0)` + the 90 ms trailing-silence pause) → audio frame. The WS path streams
sentence-by-sentence with first-sentence holdback (`_AudioPipeline` / `_synth_sentence_b64`); the
phone (`synth_to_pcm`) and local (`speak`) paths concatenate.

- **Deeper normalization (§3)** slots into the **same place Part 1 lives** — inside / called by
  `_clean_for_speech` — applied to the spoken text only, before `_synth`. Centralized → every path
  benefits, HUD untouched. (num2words call + the regex map would live here.)
- **Pauses (§2)** live in `_synth`'s trailing-silence step: make `INTER_SENTENCE_PAUSE_MS`
  punctuation/structure-aware rather than a flat 90 ms.
- **Length-variable rate (§1)** is decided **once per reply, before per-sentence synth** (in
  `synth_to_pcm` / `speak` / the streaming setup): pick a `speed` from total reply length, then pass
  it into `k.create(speed=…)`. Keep the rule simple and the delta small.

**Priority order for the future batch (highest payoff first, all free):** (1) **ear-phrasing in the
persona** (biggest win, zero latency), (2) **structural pauses**, (3) **deeper normalization
(num2words + regex)**, (4) **gentle length-variable rate**. Phrasing and pauses beat the rate knob.

---

## Sources
- Kokoro speed/quality: [Kokoro TTS guide (Clore.ai)](https://docs.clore.ai/guides/audio-and-voice/kokoro-tts) · [Kokoro review 2026 (ReviewNexa)](https://reviewnexa.com/kokoro-tts-review/) · [TTS leaderboard / Kokoro naturalness (CodeSOTA)](https://www.codesota.com/text-to-speech)
- Pauses / writing for the ear / voice UX: [VUI best practices (Aufait UX)](https://www.aufaitux.com/blog/voice-user-interface-design-best-practices/) · [How to add pauses & control pacing in TTS (Voice Creator Pro)](https://voicecreator.pro/blog/how-to-add-pauses-in-text-to-speech) · [Writing for the ear / VO scripts (Motion Array)](https://motionarray.com/learn/ai-voice-over/voice-over-scripts/) · [Voice UI design 2026 (Eleken)](https://www.eleken.co/blog-posts/voice-ui-design) · [Voice Principles (Clearleft)](https://voiceprinciples.com/)
- SSML (principles, not directly usable in Kokoro): [W3C SSML 1.1](https://www.w3.org/TR/speech-synthesis11/) · [Google SSML reference](https://developers.google.com/assistant/conversational/ssml) · [SSML prosody guide (SpeechGen)](https://speechgen.io/en/node/prosody/)
- Text normalization: [num2words (PyPI, LGPL)](https://pypi.org/project/num2words/) · [NeMo-text-processing (GitHub, Apache-2.0; Pynini/Windows note)](https://github.com/NVIDIA/NeMo-text-processing) · [NeMo WFST TN docs (NVIDIA)](https://docs.nvidia.com/nemo-framework/user-guide/24.09/nemotoolkit/nlp/text_normalization/wfst/wfst_text_normalization.html) · [NeMo ITN paper (Interspeech 2021)](https://www.isca-archive.org/interspeech_2021/zhang21ja_interspeech.pdf)
