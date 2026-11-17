# Cross-Repo Scan: What Else on This Account Is Useful for Video Editing

**Date**: 2026-11-17
**Scope**: all 75 repositories on `github.com/swathigampa354-ship-it`, private ones included
**Method**: GitHub API tree/commit reads (no bulk clone; the largest repos are 178 MB–536 MB),
line-level inspection of the video-relevant code, and a capability diff against this repo's
own pipeline in `backend/src/media/`.

---

## 0. Private repos: there are none

Every one of the 75 repos on the account is **public**. The git credential available here has
no private-repo scope (`X-OAuth-Scopes` comes back empty), and `user.total_private_repos` is
null, so a private repo would not be visible even if one existed. Nothing is being hidden from
this scan — there is simply no private corpus. If you expect private repos to be reachable,
the GitHub connection in Arena needs to be re-authorized with repo scope.

---

## 1. What SupoClip already covers (so we don't rebuild it)

`backend/src/media/` is ~7.5k lines and is stronger than most of the sibling repos:

| Area | Where | Notes |
|---|---|---|
| Reframing / camera path | `reframing.py` (1643 ln), `speaker_framing.py` (1028 ln), `scene_context.py` | YuNet face model shipped in-repo, layout snapping, Ken Burns, split-screen |
| Captions | `captions.py` (633 ln), `caption_templates.py`, `font_registry.py`, `emoji_captions.py` | ASS/libass burn-in, word-sync, safe-area positioning, emoji codepoint checks |
| Timeline / keep ranges | `timeline.py` (1107 ln) | Word-level cut mapping, crossfades |
| Ingest | YouTube/Twitch/Kick downloaders + Apify fallbacks | |
| Selection | `ai.py` (1722 ln) | LLM transcript analysis + rerank + B-roll planning |
| Editor & exports | `services/editor_service.py`, `clip_export_service.py` | Trim/split/merge, platform presets |
| Publishing | `social_service.py` + platform clients | YouTube/TikTok/Instagram |

So the interesting question is not "what is out there" but **which gaps these repos close**.
Five gaps stand out, listed in §3.

---

## 2. Full inventory, classified

### Tier A — same problem domain, directly reusable

#### `podcli` — AI podcast clipper (AGPL-3.0, same license as this repo, upstream `nmbrthirteen/podcli`)

The closest sibling to SupoClip and the single richest source. It solves several things this
repo has not:

| Capability | File(s) | Why it matters here |
|---|---|---|
| **FCPXML 1.10 export → DaVinci Resolve / FCP / Premiere** | `backend/services/integrations/_shared/fcpxml.py`, `.../_shared/timeline_ir.py`, `.../davinci_resolve/emitter.py` | SupoClip exports are locked to platform presets. This ships source on V1 + ProRes 4444 alpha caption overlay on V2 inside a compound clip, free-Resolve-safe (they document which FCPXML features watermark free Resolve). "Finish it by hand" is the #1 pro-editor request. |
| **Thumbnail studio** | `backend/services/thumbnail_generator.py` (822 ln), `thumbnail_ai.py`, `thumbnail_html.py`, `remotion/src/...` | 1080×1920 PNG variants: subject cutout, two-line title (white + accent line), logo, gradient scrim; brand config JSON. SupoClip only has a clip-cover picker in the UI. |
| **Audio-event detection (laughter / cheering / applause / screaming)** | `backend/services/audio_events.py` + `backend/models/yamnet.onnx` (model already in their repo) | YAMNet as a self-contained ONNX graph — no torch, no TF, no librosa. Reaction onsets are the strongest language-independent hook signal, and they expand a clip *backwards* to the cause. |
| **Saliency fusion + content profiles** | `saliency.py`, `profiles.py`, `audio_analyzer.py`, `signal_cache.py` | Per-video robust z-normalization (median/MAD) over transcript, prosody, audio events, energy, motion, face-reaction, with `candidate_source: "llm" \| "saliency"`. This is how they clip party/action footage with no usable transcript. SupoClip is 100% LLM-transcript dependent today. |
| **Silence removal that never cuts known words** | `silence_removal.py` | Silero VAD output is *unioned* with transcript word ranges before cut planning, and the transcript is remapped onto the compacted timeline. Good pattern for our `timeline.py` dead-air handling. |
| **Four caption styles incl. React renderers** | `backend/config/caption_styles.py`, `remotion/src/components/{Hormozi,Karaoke,Branded,Subtle}Captions.tsx` | Spring-physics Hormozi captions are hard in ASS. Ours are ASS-only, so this is the escape hatch if a customer wants that look. |
| Motion filters, saliency, face-track helpers | `motion_filters.py`, `face_track_helpers.py`, `face_analysis.py` | Small, readable, ONNX-based. |

#### `clipforge` — earlier AI clipping SaaS (no license declared → treat as read-only reference)

A previous attempt at the same product; the audit in that repo says ~40% real, many stubs
(broken workers, stubbed billing/renders). But two things are genuinely good and license-free
to *imitate*:

- `api/services/ai/analysis_engine.py` — 8 `ClipStrategy` variants (sentence, topic change,
  Q&A, strong opening, surprising claim, emotional peak, story payoff, complete thought), a
  10-dimension `ScoringEngine` (hook, information density, emotional intensity, novelty,
  narrative completeness, curiosity, quotability, relevance, minus dead air / context
  dependency), a `DeduplicationEngine` with temporal + content similarity, and a
  `ScoringWeights` config. Deterministic, testable, no LLM in the loop.
  → **Best available fallback/complement for `backend/src/ai.py`**: run it as a second opinion
  when the LLM returns thin or malformed candidates, and as a cheap re-rank before the LLM
  rerank pass.
- `api/services/reframe/reframe_engine.py` — `FaceDetector` with MediaPipe Face Mesh +
  OpenCV fallback, `SpeakerTracker` using mouth-aspect-ratio **plus audio energy**, and a
  `CameraPath` with smoothed keyframe interpolation.
  → Compare against our `speaker_framing.py`; the MAR+energy speaker vote is cheap and could
  improve multi-speaker switching.
- `docs/OPUSCLIP_GAP_MATRIX.md` + `docs/REPOSITORY_AUDIT.md` — a ready-made feature checklist
  to benchmark against, including what they found impossible (e.g. object tracking, manual
  crop, screen-share preservation).

#### `vertical-video-edits-2026-10-16` — real delivered 9:16 edits + a QC harness (no license declared → reference)

Not a library, but the best **process** artifact on the account: three finished 1080×1920
variants (30/60 fps) plus the evidence trail of how they were verified.

- `ai-work-session/references/scripts/verify_media.py` — full OpenCV decode loop asserting
  resolution, exact frame count, decoded-frame count == metadata count, plus SHA-256 and Git
  blob comparison. A ~40-line version of this belongs in our export path and CI; today we trust
  the encode.
- `ai-work-session/references/evidence/analysis/export-qc.json`,
  `editorial-v2-export-qc.json`, `final-media-info.txt` — the QC report shape worth copying
  (dimensions, fps, frames, loudness, audio sync).
- `COMMANDS_AND_TOOLS.md` — the encode recipe that worked: `libx264`, preset `fast`, `crf 18`,
  `yuv420p`, BT.709 tags, `-movflags +faststart`, and explicit
  `scale=in_range=full:out_range=tv:out_color_matrix=bt709` on the upscale leg (the fix for
  washed-out 640×360 → 1080×1920 sources). Also: mux video and mixed audio as separate inputs
  with `-map 0:v:0 -map 1:a:0 -map_metadata -1 -c:v copy -c:a aac -b:a 192k -ar 48000`.
- `references/source-snapshot/assets/fonts/` — Barlow, Barlow Condensed (Black Italic,
  SemiBold), Geist, with OFL licences included. Useful for caption template expansion.
- The scene-plan markdown format (`scene-plan-morph-v3.md`) is a good spec for a
  "graphics/animated B-roll" stage.

#### `MoneyPrinterTurbo` (MIT, fork) — faceless short-video generator

Different product (topic → video), but the pieces are reusable service code:

- `app/services/subtitle.py`, `app/services/voice.py` (Azure + ElevenLabs TTS), `bgm.py`,
  `elevenlabs_music.py`, `sonilo.py` → a "generate from topic" mode if we ever add one.
- `app/services/utils/video_effects.py` — MoviePy 2.x fades and **explicitly reimplemented
  slide transitions** because MoviePy's built-in `SlideIn` silently no-ops on full-screen
  clips. Documented workaround worth knowing.
- `app/services/material.py` + `material_cache.py` — stock material search/cache, comparable to
  our Pexels B-roll path in `backend/src/broll.py`.
- Caveat: 536 MB repo and MoviePy 2.x API. Read via API, don't clone.

#### `TiktokAutoUploader` (MIT) — upload + pre-processing

`tiktok_uploader/Video.py` does MoviePy crop/scale onto a 1080×1920 canvas with a text banner;
`tiktok_uploader/Browser.py` + `cookies.py` + the `novnc/` container handle cookie export and
human-in-the-loop login. SupoClip already publishes, so the value here is the **login/cookie
recovery flow** when a platform session dies, not the editing.

---

### Tier B — post-production model repos (all forks of upstream; GPU-flavoured)

These are not editors, but each is a plug-in for our render stage:

| Repo | Upstream | What it buys us |
|---|---|---|
| `rembg` | `danielgatis/rembg` | Subject matting → "text behind the speaker", clean B-roll compositing, thumbnail cutouts without a green screen |
| `Real-ESRGAN` | `xinntao/Real-ESRGAN` | Upscale 640×360-class sources (exactly the case in `vertical-video-edits`) before vertical crop |
| `CodeFormer` | `sczhou/CodeFormer` | Face restoration after crop+upscale on small faces |
| `lama` | `advimman/lama` | Inpainting: remove logos, lower-thirds, burnt-in old subtitles, dust on the frame |
| `InfiniteTalk` | `MeiGen-AI/InfiniteTalk` | Image + audio → talking video. Dubbing, avatar B-roll, fixing a cluttered shot |
| `Motion2MixamoRig` | `MaxLiu22/Motion2MixamoRig` | Video → Mixamo-rigged 3D character motion. 3D inserts/avatars from a phone clip |
| `PhotoMaker` | TencentARC | Identity-preserving stills for thumbnails/posters |
| `face_reaging` | timroelofs123 | Age-shift a face in thirds (niche; continuity/story gags) |

Practical note: these are heavy (torch + checkpoints, multi-GB), all of them are upstream forks
with their own licences (mostly non-commercial-research for the model weights — verify before
shipping a hosted feature), and none belong in our main worker image. If adopted, they belong
behind an optional render-tier service, not a dependency of `backend/`.

---

### Tier C — ingest, distribution, and metadata

| Repo | Use |
|---|---|
| `streambert` (GPL-3.0, fork), `MovieBox-Tui`, `PipePipe*`, `media-grabber` | Alternative download paths when yt-dlp loses a site; `PipePipeExtractor` is a maintained extractor library |
| `trypost` (AGPL-3.0), `social-mcp` (MIT), `agentp`, `AutoSocial`, `TiktokAutoUploader` (MIT) | Multi-network scheduling/publishing + MCP surfaces. Compare with our `social_service.py`; `trypost`'s composer/queue model is the most mature |
| `Open-AI-UGC` | Script → AI UGC ad variants across 4+ video models. Ad-shaped output, adjacent to our clip output |
| `awesome-opus5-5-videos` (MIT) | **703 prompts** in `prompts/` for code-driven animations (HTML/Canvas/SVG/Three.js) + `data/videos.json`. A ready source for the animated-graphics layer that the `morph v3` edit in `vertical-video-edits` was clearly hand-building |
| `deepseek-openai-title-variants` | 50 renders of one clip differing only in headline text — i.e. a title A/B harness with a manifest. Pattern for our hook-title testing |
| `tiktok-posting-memoir` | Posting operation log, tracking ledger, and a posting skill doc. Distribution learnings, not code |
| `Handy` (fork, Rust), `whisper-android-audit` | Local STT. `Handy` is a desktop dictation app; the interesting bit is its whisper.cpp/ONNX provisioning, relevant to our "fully local" mode |
| `build-with-ai-basics-research`, `repot` | Earlier audits/gap analyses |

### Not relevant to video
`brainrot-*`, `swipewipe-*`, `ShadowCore`, `termux-*`, `netflix-ui*`, `vpn-extension`,
`youcam-vto-app`, `live-vto-prototype`, `strand-hairtryon`, `tryon-studio`, `wardrowbe`,
`face-ahead*`, `future-face`, `glow-skin`, `chroma`, `live-beauty-score`, `chat-history`,
`gridcraft`, `DotDays`, `LifeGrid`, `jev-ultrafast`, `llm-council`, `screenshot-to-code`,
`YouTube-Viewer`, `faithwall-india-research`, `universal-modder`, `vscode-files`, `Zip`, `b`,
`accuserve`, `agent`, `netflix-ui-apk`, `OPEN-CLAW`, `APK_swipewipe_lm_arena`,
`tailsy-workbook-*`, `termux-debian-rootfs-*`, `android-workspace`.
(Some are strong projects — they just have nothing to do with cutting video.)

---

## 3. The five gaps worth closing, in priority order

1. **Export QC** (`verify_media.py` pattern). Cheap, unblocks trust in every other feature:
   after each render, assert dimensions/fps/frame count by decoding, check the audio stream
   exists and is in sync, and persist a QC JSON next to the export. Fits
   `backend/src/services/clip_export_service.py`.
2. **Timeline handoff (FCPXML)**. Port `_shared/fcpxml.py` + the Resolve emitter concept:
   source + alpha caption overlay as separate layers in a compound clip. New route beside
   `backend/src/api/routes/clip_exports.py`, new preset value. Highest differentiation vs
   OpusClip, and podcli already solved the free-Resolve watermark traps.
3. **Non-transcript signals** (`audio_events.py` + `profiles.py` + `saliency.py`). Add laughter/
   cheer onsets as a scoring channel in the clip-selection prompt and as a backwards clip
   expander; add a deterministic `ScoringEngine`-style scorer (clipforge's 10 dimensions) as
   the fallback when the LLM fails or returns thin output. Directly improves hook quality.
4. **Thumbnail/poster studio** (podcli `thumbnail_generator.py`). Per-clip 1080×1920 poster
   variants with brand config; feeds the publishing flow and the share page.
5. **B-roll graphics generator** (from `awesome-opus5-5-videos` + the morph-v3 scene-plan
   format). Render code-driven animated inserts for the first 2 seconds instead of stock
   footage. Bigger lift, biggest "looks expensive" payoff.

---

## 4. Licence reality check

| Repo | Licence | Reuse posture |
|---|---|---|
| `podcli` | AGPL-3.0 | Same licence as SupoClip → code can be ported with attribution. Keep the header/attribution and note it in `NOTICE`-style docs |
| `clipforge`, `vertical-video-edits-2026-10-16` | none declared | No licence = all rights reserved. Read for design, reimplement; don't copy files |
| `MoneyPrinterTurbo`, `TiktokAutoUploader`, `social-mcp`, `awesome-opus5-5-videos` | MIT | Copy with copyright notice |
| `trypost` | AGPL-3.0 | Same as podcli |
| `streambert`, `PipePipe*` | GPL-3.0 | Linking into our AGPL backend is compatible; keep it in a separate process if unsure |
| `Real-ESRGAN`, `CodeFormer`, `lama`, `PhotoMaker`, `rembg` | upstream licences | Weights are often non-commercial/research. Verify per-model before a hosted feature |

Repo size warning for anyone repeating this scan: `MoneyPrinterTurbo` 536 MB,
`streambert-workspace-archive` 249 MB, `InfiniteTalk` 235 MB, `PipePipe` 258 MB,
`vertical-video-edits` 178 MB. Use the API, not `git clone`.
