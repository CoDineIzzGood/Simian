# Simian / Project C.H.I.M.P. — Flourishin backlog

Structured backlog captured from in-session notes and live Windows
runtime observations. Ordered by priority tier (P0 runtime-critical →
P4 future intelligence). Each task has: **title**, **priority**,
**category**, **description**, **acceptance criteria**.

Format is plain markdown so it can be pasted into Flourishin's import
UI or read raw by the team while a native importer is offline.

Last updated: 2026-04-26 (Pass V).

---

## Pass V — Replay audio quality + desktop audio upgrade (latest, paste at top of Flourishin)

User report after Pass U: video, mic mux, STT, TTS and theme are all
stable. Two rough edges remain:

1. **Mic clip audio is quiet and noisy.** The export now contains an
   audio track but speech sits ~ -22 dBFS with audible room hiss. The
   user wants it both louder and cleaner without having to fiddle
   with system mic levels.
2. **Desktop audio is still hit-or-miss.** WASAPI loopback support
   varies by sounddevice build, Stereo Mix is disabled by default on
   Win11, and we never told the user *which* path actually works on
   their hardware.

Strict rules from prior passes hold: snapshot/mux untouched, GUI /
theme / STT / replay video pipeline untouched, surgical patches only.

### Root cause summary

1. **No mic preprocessing.** The fallback recorder writes raw PCM to
   WAV; the export step muxes that WAV verbatim. Anything quieter
   than the AAC codec's noise floor stays quiet, and any low-level
   hum gets carried through unchanged.
2. **No AV duration check.** The Pass U mux uses ``-shortest`` which
   already prevents a long trailing audio tail, but nothing measured
   the actual drift -- so when ffprobe reported a 12.0s video and a
   12.7s mic WAV the operator had no way to know whether ``-shortest``
   was working correctly or whether the mic was capturing into the
   future.
3. **No structured desktop-audio strategy.** ``pick_best_system_audio_choice``
   returned a single string sentinel; there was no "what paths exist
   and which one am I using" summary anywhere in the logs. Users
   without VB-Cable / Stereo Mix had to grep ``WASAPI loopback not
   supported`` and infer the rest.
4. **Diagnostics scattered across the log.** dshow enum, sounddevice
   list, mic auto-pick, WASAPI probe, and rung-fallback all logged
   from different code paths at different times. Operator had to
   stitch them together by hand.
5. **Final-export log lacked metadata.** Pass U-B verified an audio
   stream exists; Pass V wants codec / sample rate / channels /
   duration in the same line so the operator can spot wrong-format
   audio (e.g. 16 kHz mono when the mux expected 44.1 kHz) before
   the file even plays.

### Task A — Mic normalize + noise gate

`services/audio_fallback_recorder.py` adds a top-level
``preprocess_mic_wav(src, log_cb)`` returning
``(out_path, gate_applied, norm_applied, peak_before, peak_after)``.

* **Gate.** Envelope-followed (one-pole IIR, alpha=0.99 ≈ 10 ms attack/
  release) with a soft knee from ``GATE_FLOOR=0.012`` (~ -38 dBFS) to
  ``2*GATE_FLOOR``. Below the floor, gain drops to
  ``GATE_ATTEN=0.15`` (~ -16 dB) -- audible enough that the gate
  doesn't sound like a hard mute, low enough to mask room hiss.
  Skipped entirely when the input peak is below 1.5× the gate floor
  (no real signal to preserve).
* **Normalize.** Single linear gain to ``NORM_TARGET=0.95``
  (~ -0.45 dBFS), bounded by ``NORM_MIN_PEAK=0.04`` (don't amplify
  the noise floor of an empty room) and ``NORM_MAX_PEAK=0.85``
  (already loud enough; avoid clipping).
* **Failure mode.** Any exception returns the source path unchanged
  with both flags False. The original WAV is never overwritten so
  snapshot rotation can't race the preprocessor.
* **Log line.** Single contiguous line per call:
  ``[ReplayAudio] mic preprocess: gate=on, normalize=on; peak_before=0.10, peak_after=0.95 -> <stem>_processed.wav``

`services/replay_buffer.export_last` calls ``preprocess_mic_wav``
right before the mux step and uses the returned processed path when
either pass actually ran. The summary log line
(``Mic preprocess summary: gate=..., normalize=..., peak_before=..., peak_after=...``)
fires unconditionally so the operator can confirm the preprocessing
ran every time.

### Task B — AV duration drift check

* `_probe_media_duration(ffmpeg, path)` uses ffprobe (with the same
  bundled-vs-PATH resolution as the audio probe) to get container
  duration in seconds.
* `export_last` probes the post-concat video and the (possibly
  preprocessed) mic WAV and logs:
    - ``[ReplayAudio] AV duration drift: video=Xs mic=Ys drift=Zs.
       ffmpeg -shortest will trim trailing audio so the clip stays
       in sync; video is never cut.`` (drift > 0.75s)
    - ``[ReplayAudio] AV duration check: video=Xs mic=Ys drift=Zs
       (within 0.75s).`` (drift ≤ 0.75s)
* The mux command already had ``-shortest``; Pass V leaves it in
  place. Video is never cut.

### Task C — Desktop audio strategy detection

`services/audio_devices.py` adds:

* `_wasapi_loopback_supported()` -- introspects
  ``WasapiSettings.__init__`` for the ``loopback`` parameter without
  opening a stream.
* `detect_desktop_audio_strategy()` -- returns a structured dict
  with ``stereo_mix``, ``what_u_hear``, ``vb_cable``,
  ``wasapi_loopback``, ``available`` (ordered list, most reliable
  first), ``preferred``, and ``diagnostic_message``. The message
  spells out the install/enable steps when nothing works:
  ``Desktop audio unavailable: install/enable Stereo Mix (Windows
  Sound -> Recording -> right-click -> Show Disabled Devices ->
  enable 'Stereo Mix') or install VB-Cable
  (https://vb-audio.com/Cable/). Mic audio will still record.``
* Ordering: VB-Cable / VAC > Stereo Mix > What U Hear > WASAPI
  loopback. WASAPI is last because its build-version dependency
  makes it the path most likely to disappear silently after a
  sounddevice upgrade.

Mic capture is **completely independent** of desktop strategy --
the mic preprocessing path doesn't even reference the desktop probe,
so the user keeps mic audio when the desktop path is unavailable.

### Task D — Replay-start device diagnostics block

New `_emit_device_diagnostics()` method on `ReplayBufferRecorder` runs
at the very top of `start()` and emits one bracketed log block:

```
[ReplayAudio] Device diagnostics start
[ReplayAudio]   dshow audio: <name>            (one line per device)
[ReplayAudio]   sounddevice in [N]: <name> *default
[ReplayAudio]   sounddevice out[N]: <name> *default
[ReplayAudio]   selected mic (auto-pick): '<name>' (sd index N)
[ReplayAudio]   blocked from auto-pick: <comma-separated names>
[ReplayAudio]   desktop strategy: preferred='<name>'; available=[...]
[ReplayAudio] Device diagnostics end
```

Best-effort: each sub-call is wrapped so a missing dependency
(sounddevice not installed, dshow enum failure, etc.) leaves the
block in a parseable state with a "(none)" / "enumeration failed"
sub-line instead of derailing replay startup.

### Task E — ffprobe export verification with full metadata

`_probe_final_audio_stream` now returns
``(has_audio, codec, samplerate, channels, duration)``. The final
verification log line spells everything out:

* Success with one source:
  ``[Replay] Final exported streams verified: video + mic (codec=aac, sr=44100Hz, ch=1, dur=12.45s).``
* Success with both sources:
  ``[Replay] Final exported streams verified: video + mic + desktop (codec=aac, sr=44100Hz, ch=2, dur=12.45s).``
* Audio missing despite intent:
  ``[Replay] Audio skipped at export: ffprobe confirms final MP4 has NO audio stream despite the mux step succeeding. Likely causes: ...``
* Plain video:
  ``[Replay] Final exported streams verified: video only.``

Backwards compatibility note: Pass U callers that unpacked
``(has_audio, codec)`` need to switch to the 5-tuple. Inside the repo
the only caller is `export_last`, already updated. The Pass U harness
got a tiny tuple-unpack widening so it stays a regression baseline.

### Test checklist (synthetic harness)

`_pass_v_harness.py` exercises seven flows in isolation -- no GUI,
no real audio devices:

1. `preprocess_mic_wav` on a quiet (peak=0.10) tone WAV: must return
   a sibling processed WAV with peak ≥ 0.94, gate=on, normalize=on.
2. `preprocess_mic_wav` on a silence WAV: must return the source
   path unchanged with both flags False.
3. `preprocess_mic_wav` on a loud (peak=0.95) tone WAV: must skip
   normalization (gate may still run for hiss removal but is a
   no-op above the floor).
4. `detect_desktop_audio_strategy` with synthetic dshow + sd device
   lists: must rank VB-Cable above Stereo Mix and emit a setup hint
   when neither is present.
5. `_probe_final_audio_stream` against a real ffmpeg-muxed MP4: must
   return ``(True, "aac", 44100, 1, ~1.0)``.
6. `_probe_media_duration` agrees with `wave` for a known WAV.
7. `_emit_device_diagnostics` produces a single bracketed block with
   start/end markers and a "blocked from auto-pick" line listing
   Microsoft Sound Mapper.

All seven pass against bundled `ffmpeg 4.4.2 + ffprobe 4.4.2`. On
the user's Windows box they run against bundled FFmpeg 7.1.1.

### Test checklist (real Windows runtime)

For Alex on the live machine:

1. Say "simian clip that".
2. Confirm the exported MP4 plays.
3. Confirm mic voice is louder + cleaner than pre-Pass V (compare
   against any clip from the Pass U era).
4. ``grep "snapshot rotating"`` in the log -- one line per export.
5. ``grep "Final exported streams verified"`` -- must list video +
   mic and a codec/sr/ch/dur tuple.
6. ``grep "Device diagnostics"`` -- check the desktop strategy line:
   either it lists at least one path (VB-Cable / Stereo Mix /
   WASAPI), or it shows the "Desktop audio unavailable: install/
   enable..." setup hint.

### Risks + follow-ups

* **Gate over-attenuation in noisy rooms.** The current
  ``GATE_ATTEN=0.15`` (-16 dB) is mild. If the user hears the gate
  pumping or hears it cut consonants, expose ``GATE_ATTEN`` and
  ``GATE_FLOOR`` as Settings entries. Out of scope for V; in scope
  for W if it actually annoys them.
* **Two-pass amplitude analysis.** Preprocessing currently does a
  single pass over the WAV (load, gate, normalize, write). On
  long recordings (15+ minutes) the load step is the dominant cost.
  Acceptable for "clip that" use cases (typically <60s). Pass W
  could stream-process if the user wants longer clip limits.
* **VB-Cable autoinstall.** Out of scope. Pass V surfaces the install
  hint clearly enough for Alex to act on, but we don't try to
  download/install VB-Cable for them.
* **Stereo desktop capture.** When desktop comes via a stereo path
  and mic is mono, amix downmixes to mono. If the user wants the
  desktop track preserved as stereo and the mic centered, we'd need
  a separate filter graph (`amerge` + `pan`). Out of scope for V;
  flag it for W if the dual-source case becomes common.

---

## Pass U — Replay buffer audio capture + muxing (paste at top of Flourishin)

User report after Pass T: voice UX is solid (wake grace + junk gate +
route labels all working), but exported replay clips are still silent.
The runtime log line ``[Replay] Final exported streams: video (1
input)`` is the smoking gun -- the recorder thinks audio is being
captured but the export ladder can never see a finalized WAV, so every
clip ships without mic audio (and without desktop audio when
``WASAPI`` loopback is unsupported, which is most stock Win11 boxes).

Strict rules: GUI/theme/STT/replay video pipeline are untouched. Pass
U is **surgical** patches across exactly two service files
(``audio_fallback_recorder.py`` and ``replay_buffer.py``) -- no
rewrite of either, no new modules, no schema changes.

### Root cause summary

1. **Snapshot gap.** ``ReplayBufferRecorder._last_fallback_paths`` was
   populated only by ``stop()``. ``export_last`` runs while the buffer
   is *still recording*, so it always saw ``None`` and produced a
   video-only clip even when the fallback recorder was actively
   writing to ``audio_mic_*.wav``. Fix: a new
   ``AudioFallbackRecorder.snapshot()`` method that finalizes the
   in-flight WAV (so ffmpeg can demux it) and immediately re-arms a
   fresh capture window. Same-millisecond rotation is safe because
   the timestamp helper uses ``datetime.now().strftime("%f")``
   (microsecond precision).

2. **time.strftime ``%f`` was a no-op.** The previous timestamp logic
   was ``time.strftime("%Y%m%d_%H%M%S_%f")[:-3]``, which the C library
   leaves as a literal ``%f`` -- two starts in the same wall-clock
   second produced identical filenames. Replaced with a
   ``_ts_with_micros()`` helper using ``datetime`` so snapshot's
   stop+start cycle always rotates to a fresh path.

3. **No probe of the final MP4.** The export step logged what it
   *intended* to mux (``video + fallback-mic``) but never verified
   the result. If amix dropped a zero-length WAV or ``-shortest``
   clipped audio to zero, the clip silently shipped silent. Fix:
   ``ReplayBufferRecorder._probe_final_audio_stream`` runs ``ffprobe``
   (or falls back to ``ffmpeg -i`` parsing) against the exported MP4
   and logs ``Final exported streams verified by ffprobe: video +
   audio (codec=aac)`` on success, or a loud failure line on regress.

4. **Bad device picks broke fallback capture.** PortAudio defaults
   would happily route through ``Microsoft Sound Mapper - Input``,
   ``Stereo Mix``, ``PC Speaker``, or a ``Bluetooth Hands-Free``
   profile -- all noise floors or off-device. Fix:
   ``pick_best_mic_device`` scores by host API
   (WASAPI > DirectSound > MME), boosts names containing
   "microphone", honors ``sd.default.device`` as a tiebreaker, and
   refuses any device whose name matches the
   ``MIC_AUTOPICK_BLOCKLIST``. Companion ``list_alt_mic_devices``
   returns retry candidates (also blocklist-filtered) so the capture
   loop walks past silent devices on its own without operator
   intervention.

### Task A — WAV health log + silent-device retry

* ``audio_fallback_recorder.py`` instruments each capture loop with
  numpy peak tracking. ``_capture_loop`` now returns
  ``Tuple[int, float]`` (frames written, peak amplitude) and the
  outer ``_capture_loop_runner`` logs ``[ReplayAudio] WAV health ->
  path=..., duration=2.31s, samplerate=44100, peak=0.142, size=...
  bytes.``
* When peak < ``SILENCE_PEAK_THRESHOLD`` (≈ -46 dBFS) or 0 frames
  were written, the loop walks ``list_alt_mic_devices`` and retries
  with a fresh WAV path per attempt. Each retry logs the device
  decision so an operator can grep for ``Retrying mic capture on
  device``.
* Silent-WAV warning fires once: ``[ReplayAudio] WAV looks silent
  after capture (peak < threshold). Continuing anyway -- mux step
  will probe the final MP4.``

### Task B — Export muxing + ffprobe verification

* ``replay_buffer.export_last`` calls ``fb.snapshot()`` when the
  fallback recorder is running so the exporter sees finalized WAVs
  for the segment window being clipped. Falls back to
  ``_last_fallback_paths`` (only useful after ``stop()``) when
  snapshot raises.
* New ``Mux inputs ready: mic=..., desktop=...`` log immediately
  before the mux command, and ``Audio skipped at export: <reason>``
  when no audio sources were present (with the specific reason --
  "fallback recorder produced no usable WAV (silent or empty after
  all retries)" vs. "no audio fallback was armed (ffmpeg/dshow rung
  succeeded with audio, or user did not request audio)").
* New ``_probe_final_audio_stream`` helper runs ffprobe (preferring
  the sibling ``ffprobe.exe`` next to the resolved ``ffmpeg``) and
  logs the verified outcome:
    - ``Final exported streams verified by ffprobe: video + audio (codec=aac).``
    - ``Final exported streams verified by ffprobe: video only.``
    - ``Audio skipped at export: ffprobe confirms final MP4 has NO
       audio stream despite the mux step succeeding`` (the loud
       regression line, with three diagnostic causes for the
       operator).

### Task C — Desktop audio resilience

The fallback's desktop-loopback path stays best-effort: WASAPI
loopback only works on certain ``sounddevice`` builds, so when the
attempt raises we now log ``[ReplayAudio] Desktop loopback
unavailable on this sounddevice build; clip will export with mic
audio only.`` instead of silently falling through. No VB-Cable
auto-install scaffolding -- that's a Pass V item if the user wants
it.

### Task D — Smart mic device selection

* ``MIC_AUTOPICK_BLOCKLIST`` blocks Microsoft Sound Mapper,
  ``Primary Sound Capture Driver``, ``Stereo Mix``, ``PC Speaker``,
  Bluetooth Hands-Free, ``What U Hear``, and ``Wave Out Mix``. These
  are noise floors or off-device routes that PortAudio has been
  observed to pick when nothing else is wired.
* ``pick_best_mic_device`` scoring (higher is better):
    - +30 WASAPI, +20 DirectSound, +5 MME (everything else 0).
    - +15 if name contains "microphone".
    - +10 if device matches ``sd.default.device[0]``.
    - +2 per input channel (cap 4) so a stereo headset beats a
      mono variant of the same physical device.
* ``list_alt_mic_devices(skip_index, log_cb)`` returns blocklist-
  filtered candidates ordered by the same score, so the silent-WAV
  retry walks them deterministically.

### Task E — STT pause/resume around fallback (verified intact)

Already wired in Pass R-C; Pass U confirms the snapshot rotation
does **not** double-trigger pause/resume. The pause flag
(``_stt_was_paused_for_fallback``) is owned by
``ReplayBufferRecorder``, set once when the screen-only rung arms
the fallback, cleared once when ``ReplayBufferRecorder.stop()``
runs. ``snapshot()`` calls ``fb.stop()`` and ``fb.start()`` on the
inner ``AudioFallbackRecorder`` only -- it never touches the outer
pause flag, so STT stays paused for the entire fallback lifetime
and is resumed exactly once.

### Test checklist (synthetic harness)

The Pass U synthetic harness (``_pass_u_harness.py``) exercises four
flows in isolation -- no GUI, no real audio devices:

1. ``pick_best_mic_device`` scoring against a synthetic device list:
   the WASAPI Realtek mic must beat the Sound Mapper, Sound Mapper /
   Stereo Mix / Bluetooth HF must NOT appear in
   ``list_alt_mic_devices``, and the USB Headset mic must.
2. ``AudioFallbackRecorder.snapshot()``: stop + start must rotate the
   WAV path (verified by filename diff) and must preserve the
   original (mic_wanted, desktop_wanted) flags.
3. ``ReplayBufferRecorder._mux_fallback_audio`` against a real ffmpeg
   black-video MP4 + tone WAV: must produce an MP4 that ffprobe
   reports as containing an AAC audio stream.
4. ``ReplayBufferRecorder._probe_final_audio_stream`` must return
   ``False`` for the silent input video and ``True`` for the muxed
   output -- proves the probe is the ground truth, not the input
   intent.

All four pass on the current sandbox (`ffmpeg 4.4.2`, `ffprobe
4.4.2`); on the user's Windows box they run against bundled FFmpeg
7.1.1 in ``ffmpeg-7.1.1/bin/``.

### Risks + follow-ups

* **WASAPI loopback API drift.** ``sounddevice`` 0.5.5 added the
  loopback flag we rely on; older builds will silently fall through
  to mic-only. Pass U logs this explicitly so the user can pin a
  newer wheel; auto-install is out of scope.
* **Snapshot audio gap.** Stop+start typically gaps ≈200-300 ms of
  audio while PortAudio reopens the device. Acceptable for an
  on-demand "clip that" trigger; intolerable for a continuous
  recording flow. Future Pass V could mitigate with a ring-buffer
  WAV writer, but only if the audio gap actually annoys the user.
* **VB-Cable / VAC autoinstall.** Out of scope. If the user wants
  reliable desktop-loopback even on hardware where WASAPI loopback
  doesn't work, install a VB-Cable virtual device and pick it as the
  default playback. Pass U logs surface the missing-loopback case
  clearly enough to act on.

---

## Pass T — Voice UX refinement + routing confidence (paste at top of Flourishin)

User report after Pass S: STT, alias normalization, local-clock
grounding, voice-triggered "clip that", replay export, and the
GUI/TTS/theme are all stable. Three remaining UX rough edges:

1. **Wake mode is too strict after wake-acknowledge.** User says
   "a simian", Simian replies "I'm listening", user says "what
   time is it" -- the listener rejects it because it lacks the wake
   word. The conversation feels broken; the user has to keep saying
   "simian" before every utterance even though the assistant
   *just* said "I'm listening".
2. **Vosk produces junk/noise transcripts.** Specific examples
   captured from the live log: "four girls ran to my head", "love
   for dan", "amazon basin". These should never reach chat.
3. **Routing logs are hard to scan.** Every accept/reject path
   already logs *something* (since Pass R-D), but the user has to
   read the surrounding context to figure out which conceptual
   route fired. They asked for explicit ``[Voice] Route: <LABEL>``
   lines so a single grep tells them the disposition.

Strict rules from prior passes hold: no GUI redesign, theme / TTS /
replay buffer untouched, ``mic_listener.py`` is patched not rewritten.

### Root cause summary

1. **No conversational continuity primitive.** Pass S-B added a
   synthetic ``wake_acknowledge`` command for bare wake utterances
   ("hey simian" -> "I'm listening"), but the listener immediately
   reverted to "wake word required" mode for the next utterance.
   There was no concept of a "follow-up grace window" carried over
   from the ack.
2. **No confidence/junk gate.** ``_handle_text`` filtered only the
   tiniest cases (``IGNORE_UTTERANCES`` -- "huh"/"uh"/"um"…) and
   relied on the wake-word check to drop everything else. Hot-mic
   mode and the new grace window both bypass that check, so junk
   from background TV / silence / mishears could leak through.
3. **Mixed accept/reject log lines, no canonical labels.** The Pass
   R-D logs mention "rejected (no wake)", "rejected (duplicate
   within 2s)", "rejected (ignore-utterance)", etc. -- all useful,
   but the user wanted a stable, parseable label for the *route*
   decision. Adding the labels also forced us to think about each
   path one more time, which is how the grace-vs-junk-filter ordering
   got nailed down.

### Tasks A — Wake grace window (5 seconds, extend on every accept)

`services/mic_listener.py` adds the grace primitive. Three pieces:

* New constant ``WAKE_GRACE_SEC = 5.0`` (top of file, near
  ``WAKE_LEADING_FILLER``).
* New instance state ``self._wake_grace_until: float = 0.0`` (in
  ``__init__``). Cleared on ``stop()`` and on hot-mode toggle so a
  stale window cannot leak across sessions.
* Three new methods:
    - ``_in_wake_grace()`` -- returns True if the timer is in the
      future. Crossing the expiry boundary logs
      ``[Voice] Wake grace expired`` exactly once and resets the
      timer to 0.0 so we never spam the log.
    - ``open_wake_grace(seconds=5.0)`` -- starts a fresh window and
      logs ``[Voice] Wake grace opened: 5s``.
    - ``extend_wake_grace(seconds=5.0)`` -- resets the window if one
      is currently open (no-op if not). Used by the GUI after
      every successful follow-up so a chain of commands keeps the
      conversational thread alive without re-prompting.

`_extract_after_wake` now takes ``in_grace: bool = False`` and
treats a true grace flag the same way it treats hot-mode: wake word
becomes optional. ``_handle_text`` consults
``self._in_wake_grace()`` once per utterance and:

* Logs ``[Voice] Wake grace accepted: <text>`` whenever the grace
  flag was the reason a non-wake-word utterance landed as a command.
* Lets the existing ``wake_acknowledge`` path emit a route label
  (``[Voice] Route: WAKE_ACK``) which the GUI then uses as the
  trigger to call ``open_wake_grace()``.

`gui/simian_gui.py` adds two thin wrappers, ``_open_voice_grace``
and ``_extend_voice_grace``. The ``_on_voice_command`` handler
calls ``_open_voice_grace`` exactly once -- on
``wake_acknowledge`` -- and ``_extend_voice_grace`` after every
other accepted voice command (clip, buffer_start/stop, screen_*).
The transcript handler ``_on_voice_transcript`` extends the grace
after both LOCAL_TIME and CHAT routes.

### Tasks B — Junk transcript filter

New constants in ``services/mic_listener.py``:

* ``VALID_SHORT_COMMANDS`` -- explicit allow-list ("clip that",
  "stop", "cancel", "yes", "no", "ok", "okay", "pause", "resume",
  "exit", "quit") so a one-word command never gets filtered as a
  fragment.
* ``JUNK_HALLUCINATIONS`` -- compiled regex with the three concrete
  user-observed phrases ("four girls ran to my head", "love for
  dan", "amazon basin"). Open list -- extend whenever the field
  log surfaces a new hallucination.

New helper ``_is_junk(text)`` returns True if the text is empty,
matches a known hallucination, or is a single token shorter than
3 chars. The allow-list is consulted first so "stop"/"yes"/"no"
never trip the length heuristic.

The filter runs **twice** in ``_handle_text``:

* First pass against the full normalized utterance, *only* if no
  ``COMMAND_PATTERNS`` regex matches first. This kills "four girls
  ran to my head" before any wake-word logic runs.
* Second pass against the post-wake-strip ``spoken`` text. This
  catches the case where Vosk attaches a hallucination to a real
  wake word ("simian amazon basin") -- after stripping "simian" the
  surviving "amazon basin" is junked.

Rejection emits ``[Voice] Rejected: low_confidence/junk transcript: <text>``
and the route label ``[Voice] Route: REJECTED_LOW_CONFIDENCE``.

### Tasks C — Route labels

Six labels, exactly as specified, all logged with the prefix
``[Voice] Route: ``:

* ``WAKE_ACK`` -- emitted in ``_handle_text`` when a bare-wake
  utterance is about to fire ``wake_acknowledge``.
* ``LOCAL_TIME`` -- emitted GUI-side in ``_on_voice_transcript``
  when ``local_clock.maybe_answer`` matches; the GUI replies
  directly without calling Ollama.
* ``CLIP`` -- emitted in ``_handle_text`` when the ``clip`` regex
  fires. Other command patterns log ``COMMAND/<name>`` so they
  remain greppable but do not pretend to be the headline route.
* ``CHAT`` -- emitted GUI-side in ``_on_voice_transcript`` for any
  transcript that isn't a local-clock query.
* ``REJECTED_NO_WAKE`` -- emitted in ``_handle_text`` when wake-word
  mode received an utterance with no wake word and the grace
  window was closed.
* ``REJECTED_LOW_CONFIDENCE`` -- emitted at every junk/length/empty
  rejection site (both pre- and post-strip).

### Task D — Preserve Pass S

Verified by direct grep: ``SIMIAN_ALIASES`` regex unchanged,
``_strip_filler`` unchanged, ``_extract_after_wake`` still strips
the wake phrase before returning, ``services.local_clock``
unchanged, ``model_context_block()`` still prepended to the Ollama
prompt in ``_send_chat``'s worker. Pass T only adds new code paths;
it does not remove any Pass S logic.

### Task E — Test checklist (all passed)

Verified with a Python harness that loads ``mic_listener.py`` and
``local_clock.py`` directly via ``importlib.util`` (services
package import requires httpx which isn't in the sandbox). Stubs
``sounddevice`` and ``vosk`` so the listener instantiates.

1. **Wake mode follow-up.** ``_handle_text("a simian")`` -> emits
   ``Route: WAKE_ACK`` and the ``wake_acknowledge`` command;
   ``open_wake_grace()`` opens 5s window and logs accordingly;
   ``_handle_text("what time is it")`` lands as
   ``Wake grace accepted`` and ``local_clock.maybe_answer``
   produces a real answer ("It's 12:27 PM."). ✅
2. **Junk filter.** All three observed hallucinations
   ("four girls ran to my head", "love for dan", "amazon basin")
   route to ``REJECTED_LOW_CONFIDENCE`` with the expected reason
   line; ``transcript_cb`` is never called for any of them. ✅
3. **simeon clip that.** Alias normalizes to "simian clip that",
   command pattern matches, route logs ``CLIP``, ``command_cb``
   fires with ``cmd="clip"``. ✅
4. **Hot mic time query.** Listener mode set to hot mic;
   ``_handle_text("what time is it")`` queues a transcript without
   needing a wake word; ``local_clock.maybe_answer`` answers from
   the GUI side. ✅
5. **Route labels visible.** Combined harness emits ``REJECTED_NO_WAKE``,
   ``WAKE_ACK``, ``CLIP``, ``REJECTED_LOW_CONFIDENCE`` in expected
   order; grace expiry logs once after a 0.5s window; "stop" alone
   is not junked during grace. ✅

### Files changed in Pass T

* ``services/mic_listener.py`` -- added ``WAKE_GRACE_SEC``,
  ``VALID_SHORT_COMMANDS``, ``JUNK_HALLUCINATIONS``, the
  ``_wake_grace_until`` instance attr, the three grace methods,
  ``_is_junk``, the route-label log lines, ``in_grace`` parameter on
  ``_extract_after_wake``. Restored Pass S behaviors untouched.
* ``gui/simian_gui.py`` -- ``_on_voice_command`` opens grace on
  ``wake_acknowledge`` and extends on every other command;
  ``_on_voice_transcript`` adds a LOCAL_TIME early-intercept that
  short-circuits to ``_chat_reply`` and extends grace; new
  ``_open_voice_grace`` / ``_extend_voice_grace`` thin wrappers
  beside the existing pause/resume helpers; ``Route: CHAT`` /
  ``Route: REJECTED_LOW_CONFIDENCE`` log lines added at the matching
  branches.
* ``artifacts/flourishin_simian_backlog.md`` -- this section
  prepended.

### Risks / follow-ups

* **5s is a guess.** If the user tells us the natural breath
  between ack and command is longer / shorter, ``WAKE_GRACE_SEC``
  is one constant to tune (or expose via ``settings_store``).
* **Junk hallucination list is small on purpose.** The three
  documented phrases catch the live cases; if more turn up in field
  logs, append them to ``JUNK_HALLUCINATIONS``. A more general
  approach would be a Vosk confidence score gate, but that requires
  a re-train pipeline we don't currently own.
* **Route: COMMAND/<name> for non-clip commands.** The user spec
  only required CLIP as a label, but every other command pattern
  also gets a stable ``COMMAND/<name>`` route line so logs remain
  uniform. If they prefer the bare names, dropping the prefix is a
  one-line change.
* **CHAT route fires even when chat is busy.** The label is logged
  before the busy check (so it's a "this was the intent" log, not
  a "this was sent" log). The accept-or-reject log line that
  follows still tells the truth about whether the chat actually
  shipped. Could be re-ordered if logs feel misleading.
* **No telemetry hook yet.** ``services.four_d_telemetry.emit``
  could publish per-route counts so the 4D Lab tab shows a "voice
  routing health" widget. Out of scope for Pass T.

---

## Pass S — STT accuracy + local-clock grounding

User report after Pass R: STT is alive and hot mic ships speech to
chat, but accuracy is poor. Vosk's small en-US model mishears the
wake word as "semi him" / "simeon" / "simion" / "symian"; commands
are messy but reach chat anyway. Separately, when asked the time
the assistant answered 8:15 AM while the Windows clock read 11:24
AM — pure model hallucination, no grounding to the local system
clock. Strict rules from Pass R hold: no GUI redesign, theme/TTS/
replay-buffer untouched, surgical patches only.

### Root cause summary

1. **Narrow alias map.** ``SIMIAN_ALIASES`` covered only 5 surface
   forms ("semyon", "semion", "semi on", "sim eon", "simiann"). The
   Vosk small model produces a longer list on the user's mic
   ("simeon", "simion", "symian", "cimian", "semi him"…), and
   non-canonical forms slipped past the wake-word check.
2. **Wake word leaked into LLM prompt.** ``_extract_after_wake``
   stripped only the literal wake word. Filler words ("hey", "a",
   "please") in front of it stayed in, and a wake-only utterance
   like "hey simian" silently dropped instead of being answered.
3. **No local-clock grounding.** Every chat turn — including
   "what time is it" — was forwarded to Ollama. The model has no
   notion of the user's wall-clock time, so it just made up a
   plausible answer (8:15 AM in the report).
4. **Prompt didn't carry today's date/time.** Indirect time-
   sensitive prompts ("what's on my schedule today") had no anchor
   either, so even when the interceptor missed, the model still
   guessed.

### Task S-A — STT alias normalization

**``services/mic_listener.py``: ``SIMIAN_ALIASES``**

Widened to 11 surface forms, with multi-token mishears
("semi him", "sim eon", "semi on") listed first so leftmost-longest
matching prefers them over single-token overlaps. The regex still
substitutes a single canonical "simian" so downstream wake / command
matching is unchanged.

```
SIMIAN_ALIASES = re.compile(
    r"\b(?:"
    r"semi\s+him|sim\s+eon|semi\s+on|"
    r"semyon|semion|simion|simeon|symian|simiann|cimian|cymian"
    r")\b",
    re.IGNORECASE,
)
```

**``_handle_text`` logging**

After normalization, every utterance now logs one of:

- ``[Voice] Normalized: raw='X' -> normalized='Y' (wake_match=True)``
- ``[Voice] Normalized: 'Y' (wake_match=False)`` (when raw == norm)

So we can prove the alias map fired and whether the wake word was
present in a single line per utterance.

### Task S-B — Wake-strip + filler-strip + wake-only ack

**``services/mic_listener.py``: ``_strip_filler`` + ``_extract_after_wake``**

New helper ``_strip_filler`` peels leading filler words from a
``WAKE_LEADING_FILLER`` set ({"hey", "ok", "okay", "yo", "please",
"a", "the", "uh", "um", "well"}) iteratively, so "hey please clip
that" reduces to "clip that".

``_extract_after_wake`` was restructured around three explicit
branches:

- **No wake configured.** Strip filler from the whole utterance.
- **Hot mode.** If wake appears in the utterance with only filler
  in front of it, return the tail with filler stripped. Otherwise
  return the whole utterance with leading filler peeled.
- **Wake-word mode.** Wake must appear; return tail with filler
  stripped. Empty result means "wake-only utterance" and is
  signaled separately (see below).

**Wake-only acknowledgement**

When the user says just "hey simian" or "simian" with nothing
after it (in wake mode), the listener now emits a synthetic
``wake_acknowledge`` command instead of dropping silently. The
GUI router catches it and replies "I'm listening." through the
existing ``_chat_reply`` path (which TTS picks up automatically).
No round trip to Ollama for that case, so the response is instant.

### Task S-C — Local time/date provider

**New module ``services/local_clock.py``**

- ``current_local()``: single ``datetime.datetime.now()`` seam.
- ``format_time(now)``: ``It's 11:24 AM.``
- ``format_date(now)``: ``Today is Saturday, April 25, 2026.``
- ``format_date_and_time(now)``: combined response.
- ``model_context_block(now)``: short prompt block carrying weekday,
  date, 12-hour clock, ISO datetime, and the assistant's name.
- ``maybe_answer(text)``: returns a plain-English answer for time/
  date queries, otherwise ``None``.
- Three regex patterns: ``TIME_QUERY_RE``, ``DATE_QUERY_RE``,
  ``DATE_AND_TIME_RE``.

Pure stdlib (datetime, re), no new dependency.

**``gui/simian_gui.py``: ``_handle_local_query``**

Tries ``local_clock.maybe_answer(text)`` BEFORE the existing health-
query check. On a hit, logs the exact user-requested line
``[Time] Answered from local system clock`` and returns the answer.
Health-query path is unchanged.

### Task S-D — Model prompt context

**``gui/simian_gui.py``: chat ``worker()``**

Every Ollama prompt now includes ``local_clock.model_context_block()``
as a top-level system block, plus a hard rule:

> Never invent the current time or date. If the user asks, use the
> Local clock context above verbatim.

Built once per turn, never raises (catches import / clock errors and
logs ``[Chat] Could not build local-clock context (…)`` while still
calling the model). The block is small (~5 lines) so it eats minimal
context on the small local model.

### Test checklist (S)

- [ ] **Wake mode "hey simian".** Log shows
      ``[Voice] Vosk raw: hey simian`` →
      ``[Voice] Normalized: 'hey simian' (wake_match=True)`` →
      ``[Voice] Wake-only utterance: hey simian`` →
      ``[Voice] GUI accepted command: wake_acknowledge``. Chat
      bubble + TTS speak "I'm listening."
- [ ] **Mishear with command.** Say "hey simeon what time is it"
      in wake mode. Log shows
      ``[Voice] Normalized: raw='hey simeon what time is it' ->
      normalized='hey simian what time is it' (wake_match=True)``
      → ``[Voice] Transcript queued for flush: what time is it`` →
      ``[Voice] GUI accepted transcript -> chat: what time is it``
      → ``[Time] Answered from local system clock`` →
      "It's 11:24 AM." (matches Windows tray).
- [ ] **Symian / Cimian variants.** "symian clip that" and
      "cimian clip that" both normalize to "simian clip that" and
      route ``[Voice] Command routed: clip``.
- [ ] **Hot-mic prefix.** In hot mode, say "simian hello". Log
      shows ``after_wake='hello'`` and chat receives just "hello",
      not "simian hello".
- [ ] **Hot-mic plain phrase.** In hot mode, say "what time is it".
      Log shows ``[Time] Answered from local system clock`` and
      "It's 11:24 AM." reaches chat.
- [ ] **Date query.** "what day is it" returns "Today is
      Saturday, April 25, 2026." and logs ``[Time] Answered from
      local system clock``.
- [ ] **Date+time combo.** "what's the date and time" returns
      both the date and the time in one reply.
- [ ] **Indirect query falls through to model.** "remind me later
      today" still hits Ollama, but the prompt now contains the
      ``Local clock context`` block so the model anchors to today's
      actual date.
- [ ] **No regression for non-time queries.** "tell me a joke" →
      no ``[Time]`` log, model handles it as before.

### Risks & next steps (S)

- **Alias coverage is observation-driven.** Different mics + Vosk
  versions produce different mishears. Add new variants to
  ``SIMIAN_ALIASES`` whenever a new pattern shows up in the
  ``[Voice] Vosk raw:`` log. Future pass: enable Vosk's
  ``SetGrammar`` with a small biased vocabulary including the
  command verbs ("clip that", "start replay", etc.) to push raw
  accuracy up before normalization.
- **Filler list is conservative.** Words like "now", "really",
  "actually" stay in because they often carry intent. If chat
  shows them as noise we can extend ``WAKE_LEADING_FILLER``.
- **Time interceptor is regex-based.** Phrases like "the time
  please tell me" outside the listed patterns will fall through
  to the model — the ``model_context_block`` covers that case so
  the model still answers correctly.
- **Locale.** ``%A %B`` use the C locale by default on Windows,
  which yields English names. If the user later wants localized
  output we can plumb ``locale.setlocale`` here behind a setting.
- **Wake-only ack volume.** "I'm listening." every time the user
  says a stray "simian" could feel chatty. If complaints arrive,
  add a 5-second debounce in ``_on_voice_command`` for the
  ``wake_acknowledge`` branch.

---

## Pass R — STT reliability + WASAPI loopback API drift (paste at top of Flourishin)

User report after Pass Q: GUI perf is acceptable, theme works, TTS
plays, replay video records, replay mic fallback starts — but the
WASAPI desktop loopback errors with
``WasapiSettings.__init__() got an unexpected keyword argument
'loopback'`` on ``sounddevice 0.5.5``, and STT is silently failing
across the board (mic button does nothing, wake word never triggers,
hot mic appears dead). Strict rules carry over from Pass Q: no
redesign, no rewrite, theme + TTS + replay video + GUI perf all
preserved. **Critical: do NOT assume a sounddevice upgrade fixes
WASAPI loopback** — must handle the unsupported-build path natively.

### Root cause summary

1. **WASAPI loopback API drift.** sounddevice 0.4.x exposed
   ``WasapiSettings(loopback=True)`` for desktop-loopback capture; the
   0.5.5 build the user is on dropped that kwarg. Calling it blindly
   raised ``TypeError`` from inside the audio fallback thread, which
   propagated up and nuked the whole desktop-audio rung.
2. **STT vs replay mic contention.** The Pass Q
   ``AudioFallbackRecorder`` opens ``sd.InputStream`` on the default
   mic device. ``MicListenerService`` already has ``sd.RawInputStream``
   open on the same device. PortAudio on Windows returns
   ``-9999 (unanticipated host error)`` to whichever opens second;
   the failure was silently swallowed via ``_last_error`` and the
   user just saw "STT not working" with no log line.
3. **Three-state mic UI was actually two-state.** ``_toggle_chat_mic``
   only flipped between Off and Hot Mic. Wake-word mode was
   reachable only via ``auto_start_mic=True`` at launch and was
   labeled "Mic Off" in the UI even when running, so the user
   reasonably concluded the mic was broken.
4. **No accept/reject log on transcripts.** Every rejection branch
   (filler utterance, duplicate, chat busy) returned silently, so a
   working listener with a stale dedup cache looked identical to a
   crashed one.

### Task R-0 — WASAPI loopback API fix (sounddevice 0.5.5)

**``services/audio_fallback_recorder.py``**

Replaced the unconditional ``WasapiSettings(loopback=True)`` call with
a runtime ``inspect.signature`` check on the constructor. If
``"loopback" in sig.parameters`` we pass it; otherwise we log
``[ReplayAudio] WASAPI loopback not supported in this sounddevice
build; desktop audio capture disabled. Mic capture will continue
normally.`` and ``return 0`` for that one stream. Mic capture is on
its own thread and is unaffected. No upgrade path required, no crash,
no aborted rung — just a clean degradation to mic-only fallback.

### Task R-A — STT verbose logging at every gate

**``services/mic_listener.py``**

The listener thread now logs the gates the user explicitly called out:

- ``[Voice] Listener thread starting; device=…(name); samplerate=…; mode=…``
- ``[Voice] Vosk model path: <absolute>``
- ``[Voice] Audio chunks received: N (rolling)`` once every 200 chunks
  (~ once every ~100s at 16 kHz / 8000-frame blocks). Tells you
  "audio is flowing" without spamming every block.
- ``[Voice] Vosk raw: <text>`` before normalization, dedupe, or wake
  filter. Distinguishes "Vosk is silent" from "we filtered it".
- ``[Voice] Heard: <heard>`` (was already there) for the normalized
  form.
- ``[Voice] Input stream opened on device=… ; listener active.``
- ``[Voice] Input stream closed for pause; awaiting resume.``
- ``[Voice] Input stream reopened after pause.``
- ``[Voice] Listener thread stopped.`` on clean exit.
- ``[Voice] Rejected (...)`` for every drop reason: empty,
  ignore-utterance, duplicate, no-wake-word, too-short.
- ``[Voice] Command routed: <name> (raw='…')`` accept line.
- ``[Voice] Transcript queued for flush: <spoken>`` accept line.

A new ``_describe_device(device)`` helper resolves the PortAudio device
name for the startup log. Best-effort, never raises.

### Task R-B — Mic Off / Wake Word / Hot Mic state machine

**``gui/simian_gui.py``: ``_toggle_chat_mic`` + ``_sync_chat_mic_controls``**

Click cycle is now Off → Wake Word → Hot Mic → Off. The button label
mirrors the live state: ``Mic Off`` / ``Wake Word`` / ``Hot Mic``.
Voice hint label below the button explains all three states. The
Wake Word → Hot Mic transition flips ``set_hot_mode(True)`` on the
existing listener instead of stop+restart so we don't drop audio
mid-sentence. ``MicListenerService.set_hot_mode`` now clears
``_last_text`` / ``_last_text_ts`` on a real mode change, so the
first hot-mic phrase right after a wake-word trigger isn't rejected
as "duplicate within 2s".

### Task R-C — STT vs replay mic conflict

**``services/mic_listener.py``: pause / resume**

Added ``pause()`` and ``resume()`` methods. ``pause()`` sets a
``threading.Event``; the inner stream loop in ``_run()`` exits its
``with stream:`` block (callback raises ``CallbackStop``), releasing
the input device. The listener thread does NOT die — it spins in a
short sleep loop until ``resume()`` clears the flag, then reopens
the input stream. Vosk model load is the expensive part (~ 200–600 ms
on cold disk), and that stays alive across a pause, so resume is
near-instant. ``stop()`` clears the pause flag too so a stop-during-
pause doesn't deadlock the join.

The exact log lines requested:
- ``[STT] paused due to replay capture`` (in ``pause()``)
- ``[STT] resumed after replay capture`` (in ``resume()``)

**``services/replay_buffer.py``: ``ReplayBufferRecorder``**

Added ``stt_pause_cb`` and ``stt_resume_cb`` constructor parameters.
``_maybe_arm_audio_fallback`` calls ``stt_pause_cb()`` right before
``AudioFallbackRecorder.start(mic_wanted=True, …)``. ``stop()`` calls
``stt_resume_cb()`` after ``fb.stop()`` returns and the WAVs are
finalized. An internal ``_stt_was_paused_for_fallback`` flag prevents
unbalanced pause/resume pairs (we only resume if we paused). A pure
desktop-loopback fallback (``mic_wanted=False``) leaves STT running
the whole time.

**``gui/simian_gui.py``: ``_pause_mic_listener_for_replay`` /
``_resume_mic_listener_after_replay``**

Two thin GUI wrappers handed to ``ReplayBufferRecorder`` at construction.
Both no-op when the listener is None or doesn't expose the new methods.
Wired in ``self.replay = ReplayBufferRecorder(log_cb=self.log,
stt_pause_cb=…, stt_resume_cb=…)``.

### Task R-D — Command flow accept/reject logging

**``gui/simian_gui.py``: ``_on_voice_transcript`` + ``_on_voice_command``**

Every transcript that reaches the GUI now ends in exactly one
``[Voice] GUI accepted transcript -> chat: …`` or ``[Voice] GUI
rejected (<reason>): …`` log line. Reasons: empty transcript,
filler utterance, duplicate within 2s, chat busy. Same shape for
the command router: ``[Voice] GUI accepted command: <name> (raw='…')``
or ``[Voice] GUI rejected (unknown command 'X', raw='…')``. Combined
with the listener-side ``[Voice] Rejected (...)`` lines, every Vosk
recognition has a complete trace from raw bytes to chat input.

### Task R-E — Audio safety preserved

No regressions to:

- DirectShow ladder ordering (Pass M auto-pick, Pass P empty-enum
  no-refuse, Pass J validation).
- Pass Q audio fallback recorder (still arms on screen-only when
  any audio was requested).
- WASAPI loopback now degrades gracefully on unsupported builds; mic
  fallback continues unaffected.
- gdigrab / segment muxer / health check chain unchanged.

### Test checklist (R)

- [ ] **Mic button cycle.** Click once → label changes to "Wake
      Word", listener thread starts, ``[Voice] Listener thread
      starting…`` appears with absolute model path. Click again →
      label "Hot Mic", listener stays running, ``[Voice] Listener
      mode: hot mic.``. Click again → label "Mic Off",
      ``[Voice] Mic listener stopped.``.
- [ ] **Wake word path.** With listener in Wake Word mode, say
      "Simian, what time is it?". Log shows ``[Voice] Vosk raw:
      simian what time is it`` → ``[Voice] Heard: …`` →
      ``[Voice] Transcript queued for flush: what time is it`` →
      ``[Voice] GUI accepted transcript -> chat: what time is it``
      → chat sends.
- [ ] **Wake word miss.** Say "what time is it" without "simian".
      Log shows ``[Voice] Rejected (no 'simian' wake word in wake
      word mode): …``. No chat send.
- [ ] **Hot mic path.** Click button to Hot Mic, say "hello there".
      Log shows raw + ``[Voice] Transcript queued for flush: hello
      there`` + GUI accepted line, chat sends.
- [ ] **Clip command.** Say "Simian, clip that" while replay is up.
      Log shows ``[Voice] Command routed: clip`` then ``[Voice]
      GUI accepted command: clip (extra=Ns, raw='…')`` then the
      export pipeline runs.
- [ ] **Replay mic conflict.** With listener running and replay
      buffer entering screen-only fallback with mic_wanted=True,
      log shows ``[STT] paused due to replay capture`` BEFORE
      ``[ReplayAudio] mic: capture started``. Listener does NOT
      die — ``Mic listener: running`` stays in the status label.
      When replay stops, log shows
      ``[ReplayAudio] mic: stopped after Ns…`` then
      ``[STT] resumed after replay capture`` then ``[Voice] Input
      stream reopened after pause.`` Speaking immediately after
      resume produces a normal transcript.
- [ ] **WASAPI unsupported build.** Replay's screen-only rung with
      ``desktop_wanted=True`` on sounddevice 0.5.5 logs
      ``[ReplayAudio] WASAPI loopback not supported in this
      sounddevice build…`` and continues without crashing. Mic
      capture WAV still records and gets muxed into the export.
- [ ] **Duplicate suppression sanity.** Say the same phrase twice
      within 1 second. First fires fully; second logs
      ``[Voice] Rejected (duplicate within 2s): …`` and stops there.

### Risks & next steps (R)

- **DirectShow path still ignores STT.** This pass coordinates only
  the Pass Q sounddevice fallback with the listener. If the dshow
  rung succeeds in opening the same physical mic, contention can in
  principle hit there too, but in practice DirectShow vs WDM-KS lets
  both reads coexist on Windows 11. If that proves wrong we'll add
  the same pause/resume hooks to the dshow rung in a future pass.
- **First-resume window.** ``AudioFallbackRecorder.stop()`` is
  bounded by a 4 s timeout (Pass Q). Resume happens after that
  returns. In the worst case there's a ~ 4 s STT silence after a
  replay clip ends. Acceptable for the v1 fix; can be tightened by
  shortening that timeout if it shows up in metrics.
- **PortAudio device renaming.** The new device-name log uses
  ``query_devices(index)``; if the index is stale (device unplugged
  between picker save and listener start) we still log the index
  and let the open call fail with the real PortAudio error.
- **Per-utterance log volume.** Three to five log lines per phrase
  is fine for debug; if it pollutes long sessions we can gate the
  per-utterance lines behind ``low_resource_mode`` or a new
  ``stt_verbose`` setting.

---

## Pass Q — Audio fallback + GUI lag refinement (paste at top of Flourishin)

User report after Pass P: replay video now works, but audio still
silent. FFmpeg/DirectShow can't open Stereo Mix even though
``sounddevice`` sees it. Separately, general GUI lag (scrolling, tab
switching, log textbox, overall responsiveness). Strict rules: no
rewrite, no GUI redesign, no removed features, theme system stays
intact. Small surgical patches only.

### Task A — Replay audio (shipped in Pass Q)

**New module ``services/audio_fallback_recorder.py``**

Captures mic + (optional) WASAPI desktop loopback to per-session WAV
files in the replay buffer dir. Lazy imports ``sounddevice`` (already
a transitive dep) and uses stdlib ``wave`` + numpy for WAV writing
so no new package is required. Each stream runs in a daemon thread,
writes mono float32->int16 at 44.1kHz, and is stopped via a shared
``threading.Event``. Stream-open failures are logged and the failing
side is silently dropped, so a missing mic never blocks desktop
capture and vice versa. ``cleanup_old_fallback_wavs`` trims to the 8
most recent WAVs on every replay start so disk doesn't grow without
bound.

**``ReplayBufferRecorder._maybe_arm_audio_fallback``**

Called from ``_emit_rung`` whenever the rung label starts with
``screen-only`` AND the user actually picked at least one audio
device. That means: ffmpeg's full→mic-only→desktop-only ladder all
failed (which is the Win11-no-VAC case), but the user wanted audio,
so we spin up the Python recorder for the streams ffmpeg gave up on.
Idempotent (won't re-arm if a recorder is already running). Logs the
arm decision clearly so an operator can see "DirectShow couldn't
open audio, but sounddevice / WASAPI loopback may still be available".

**``ReplayBufferRecorder.stop`` finalizes WAVs first**

Before ffmpeg is torn down, the fallback recorder is stopped and its
``paths()`` snapshot is stored on ``self._last_fallback_paths``.
Logs ``[Replay] Audio fallback finalized: mic=…, desktop=…`` (or
``no usable WAVs``) so the operator can correlate which WAVs the
clip will mux at export.

**``ReplayBufferRecorder._mux_fallback_audio`` + ``export_last``**

After the existing concat step produces ``tmp_out`` (silent video),
``export_last`` checks for fallback WAVs:
- one WAV → ``-map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest``
- two WAVs → ``-filter_complex amix=inputs=2`` → ``-map [aout]`` ditto
- mux failure or no WAVs → silent video preserved untouched

Final summary line ``[Replay] Final exported streams: video,
fallback-mic, fallback-desktop (3 inputs)`` (or whichever were
actually included) so the user doesn't have to grep the log to know
what made it into the clip.

**Status logs at every gate**

- ``[Replay] Available dshow audio devices: [...]`` (Pass M)
- ``[Replay] sounddevice inputs (for cross-debug): [...]`` (Pass P)
- ``[Replay] Capture rung: <label>`` (existing, four-rung ladder)
- ``[Replay] HEALTH: First segment OK / tiny / missing`` (Pass P)
- ``[Replay] Arming Python audio fallback (sounddevice)…`` (NEW)
- ``[ReplayAudio] mic: capture started -> audio_mic_TS.wav`` (NEW)
- ``[ReplayAudio] desktop: capture started -> audio_desk_TS.wav`` (NEW)
- ``[ReplayAudio] mic: stopped after Ns (N frames)`` (NEW)
- ``[Replay] Audio fallback finalized: mic=…, desktop=…`` (NEW)
- ``[Replay] Muxing fallback audio: video + N WAV input(s).`` (NEW)
- ``[Replay] Final exported streams: video, fallback-mic, …`` (NEW)

### Task B — GUI stability / lag (shipped in Pass Q)

**Log textbox cap lowered + persistent disk mirror**

``UILogger.max_lines`` reduced from 700 to 400 in the constructor
call, because every flush over cap pays an O(N) trim. Reducing the
ceiling halves the worst-case trim cost on a hot log run. Full session
log is mirrored to ``logs/simian.log`` (append mode, 8KB buffered)
with a naive 5MB rotation to ``logs/simian.log.1`` on next session
start so disk doesn't grow without bound. The disk mirror runs inside
the existing ``_drain`` so no new thread is added. Failures silenced
(losing a log line is preferable to taking down the GUI).

**Scroll-lock for the log textbox**

``UILogger._scroll_locked`` flag suspends the auto-``see("end")``
when the user touches the scrollbar. New ``SimianApp._on_log_scroll``
binds ``<MouseWheel>`` / ``<Button-4/5>`` / ``<KeyPress>`` /
``<ButtonRelease-1>`` on the log textbox; an ``after(1200, …)`` timer
re-arms auto-follow after the user stops scrolling. Means: scrolling
through history no longer fights the log appender, and a lazy user
who scrolls down and stops gets auto-follow back automatically.

**Tab switch instant-paint via ``after_idle``**

``_on_tab_changed`` now schedules first-visit lazy builds via
``self.after_idle(self._lazy_build_*)`` instead of calling them
inline. The new tab paints first (because the event handler returns
immediately), then the heavy build runs on the next idle cycle. User
sees the tab snap in even when the build itself is 100-200ms.

**Theme apply: 60ms debounce + scroll-lock guard + perf-warn**

``_apply_theme`` records ``self._theme_last_global_ts`` and short-
circuits a second global call within 60ms (catches the
"settings-apply triggers 3 reapply paths" pattern). Skipped entirely
when ``_scroll_locked`` is true and the call is global -- the user
will get the theme on the next non-scroll trigger (same widget tree,
same palette, just deferred). End of method logs
``[Perf] _apply_theme (global) took Nms`` over 50ms so a runaway tree
shows up as a one-line warning.

**Tab switch perf log**

``_on_tab_changed`` now wraps in ``perf_counter`` and logs
``[Perf] Tab switch to 'Foo' took Nms`` over 50ms.

**Existing perf logs (kept intact)**

- ``[Perf] UILogger flush avg X.Xms over 50 drains.`` (Pass N)
- ``[Perf] News tab hydrated in Nms (lazy).`` (Pass N)
- ``[Perf] 4D Lab tab hydrated in Nms (lazy).`` (Pass N)
- ``[Perf] Startup reached _auto_start at +Nms.`` (Pass N)

### Files changed

- ``services/audio_fallback_recorder.py`` — new (~230 lines)
- ``services/replay_buffer.py`` — fallback wiring, mux helper,
  ``stop()`` finalization, fresh-session reset of
  ``_last_fallback_paths``, ``Any`` added to imports
- ``gui/simian_gui.py`` — UILogger disk mirror + scroll-lock,
  textbox cap 700→400, scroll-lock bindings, ``after_idle`` tab
  hydration, ``_apply_theme`` debounce + scroll-skip + perf-warn,
  tab-switch perf-warn, ``_on_log_scroll`` /
  ``_on_log_scroll_release`` handlers

### Test checklist

1. **Win11 box, no VAC, Stereo Mix disabled** — pick a mic in the
   picker, start replay. Confirm log shows ``Capture rung: screen-only
   (...)``, then ``Arming Python audio fallback (sounddevice)``,
   then ``[ReplayAudio] mic: capture started``, then
   ``[ReplayAudio] desktop: capture started`` (the desktop one will
   succeed if WASAPI loopback is supported on the box, fail otherwise).
2. **Trigger a clip export** — confirm log shows ``Audio fallback
   finalized: mic=audio_mic_*.wav, desktop=audio_desk_*.wav``,
   then ``Muxing fallback audio: video + N WAV input(s).``, then
   ``Final exported streams: video, fallback-mic, fallback-desktop``,
   then ``Saved: clip_*.mp4``. Open the clip and confirm audio.
3. **Stereo Mix enabled or VAC installed** — confirm rung is
   ``full (desktop + mic)`` and the fallback NEVER arms (
   ``screen-only`` prefix never appears for the rung label).
4. **Mic-only sounddevice** — uninstall WASAPI loopback (or run on
   a non-Windows platform). Confirm ``[ReplayAudio] desktop: WASAPI
   loopback unavailable`` logs and capture continues with mic only.
5. **Log textbox cap** — let the GUI run for 5 minutes with hot logs;
   confirm visible textbox stays at 400 lines, ``logs/simian.log``
   grows past 400 lines.
6. **Disk rotation** — set ``_disk_max_bytes`` to 64KB temporarily,
   confirm rotation produces ``logs/simian.log.1`` and a fresh
   ``logs/simian.log``.
7. **Scroll lock** — scroll up in the log; confirm new lines stop
   snapping the viewport to the bottom. Stop scrolling; after ~1.2s,
   confirm the next new line scrolls to bottom again.
8. **Tab switch latency** — switch between tabs rapidly; confirm
   no ``[Perf] Tab switch to 'Foo' took Nms`` warnings (or only
   first-visit hydrations show up in the warn band, never re-visits).
9. **Theme debounce** — open Settings, click "Apply settings"; confirm
   the theme walk only logs at most one ``[Perf] _apply_theme`` line
   (and only if it actually exceeded 50ms). Click in rapid succession
   3x; confirm the second/third Apply does NOT trigger duplicate
   walks within 60ms.
10. **Theme during scroll** — start scrolling the log; while scrolling,
    open the Theme popup and pick a new accent. Confirm: scrolling
    stays smooth, the theme reapplies on next non-scroll input.

### Risks / follow-up

- **WASAPI loopback availability** — ``sd.WasapiSettings(loopback=True)``
  exists on sounddevice >=0.4.6 with PortAudio 19.7+. Older builds
  log "WASAPI loopback unavailable on this build". User should
  ``pip install -U sounddevice`` if desktop loopback fails. Consider
  adding sounddevice to ``requirements.txt`` (currently only listed
  via transitive deps).
- **A/V drift on long clips** — The fallback path muxes WAVs that
  may have been recording for the entire buffer window (e.g. 5
  minutes). ``-shortest`` trims to video length so the clip is the
  right duration, but if Windows's audio clock drifts vs. ffmpeg's
  gdigrab clock, the audio may sit a few hundred ms ahead/behind.
  Acceptable for replay use; a future pass could add periodic PTS
  resync if drift becomes audible on >2-minute clips.
- **Disk mirror failure mode** — ``logs/simian.log`` open fails
  silently if ``logs/`` isn't writable. A future pass should surface
  this as a one-time GUI banner instead of just a missing file.
- **Tab switch perf warn floor** — 50ms is a heuristic; the real
  smooth-feel threshold on a hot screen may be 30ms. If the warn
  log gets noisy, raise the floor.
- **Theme debounce edge case** — a debounced call dropped while
  scroll-locked needs to be re-applied after scroll-unlock. Today
  it isn't queued; the next NON-debounced theme call (e.g. settings
  apply, picker change) re-applies. Acceptable because palette
  reads at draw time pick up theme key changes anyway.

### Honest note

As with previous passes, Pass Q edits are made against the in-repo
tree but have not been executed on Alex's Windows 11 box. Diagnosis
follows from: Pass P removed the dshow hard-refuse → ffmpeg now
actually attempts the picked names → if it can't open them, the
ladder cascades to screen-only → previously this meant "no audio
period" → Pass Q closes that gap by spinning up sounddevice for the
streams ffmpeg gave up on. The Test checklist above is the minimum
set Alex should run to confirm.

---

## Pass P — Replay buffer regression fix (paste under Pass Q)

User report: replay clip opens but shows black video, no desktop audio,
no mic audio. FFmpeg log shows `enumerated zero dshow audio devices`
and the system falls back to screen-only. Strict rule: do NOT touch
the theme system, do NOT redesign GUI, do NOT remove replay — only
fix replay + ffmpeg + device handling.

### Diagnosis

1. **Black video (root cause)** — `_build_args` was passing
   `-video_size {s.width}x{s.height}` verbatim from
   `config/settings.json` defaults (1920×1080). When the live desktop
   is larger or scaled (HiDPI / 2560×1440 / 4K / rotated secondary),
   gdigrab requests a region that lies partly or entirely outside the
   compositor surface and silently returns black frames. The Pass J
   `-draw_mouse 0` + Pass K black-frame luma probe both stayed in, but
   neither guards against a size mismatch.

2. **Zero dshow audio (root cause)** — Win11 ships with Stereo Mix
   disabled and no Virtual Audio Capturer filter; `list_dshow_audio_devices`
   returns `[]`. Pass M's "hard refuse" (`if enum_ok and not available:
   return [], 0, None`) short-circuited `_audio_input_args` and the
   first rung immediately resolved to "screen-only (no dshow audio
   devices)". That was intentional to stop ladder thrash, but it made
   the mislabel of a screen-only outcome look like "we tried" when we
   never actually handed the picked names to ffmpeg.

3. **Silent failure after launch** — even when gdigrab starts and
   stays alive past the 2.0s grace, it can produce zero frames for
   driver / permission reasons. The ladder's only success signal is
   "process still running", so a black capture looked healthy until
   the user played the clip.

### Hotfix Now (shipped in Pass P)

- **`_detect_desktop_size()` in `services/replay_buffer.py`** — probes
  `mss.mss().monitors[1]` for `(width, height)` of the primary monitor.
  Returns `None` when mss is missing or the probe fails; callers treat
  `None` as "unknown, use settings verbatim".

- **`start()` computes `eff_w, eff_h`** — starts from `s.width / s.height`
  and shrinks to the detected desktop size whenever the configured
  size is LARGER. Never enlarges (a user who picked 1920×1080 on a 4K
  box presumably wants a downscale, not an upscale). Logs
  `[Replay] Desktop detected: WxH; settings: W'xH'.` then
  `[Replay] Capture size WxH exceeds desktop DxD; shrinking to DxD
  to avoid off-screen (black) capture.` when it fires.

- **Explicit `-offset_x 0 -offset_y 0` in `_build_args`** — gdigrab
  defaults to (0,0) but some driver paths were observed to honor stale
  offsets; being explicit also documents that we always record the
  primary monitor region.

- **`-thread_queue_size 1024` on the gdigrab input** — matches the
  dshow audio side so the video pipe doesn't stall under mixed input
  load.

- **Drop the dshow empty-enum hard refuse** — `_audio_input_args` no
  longer returns `[], 0, None` when `enum_ok=True and not available`.
  The flow continues; because `resolved_sys = ... if available else
  sys_choice`, the picked name is handed verbatim to ffmpeg. If the
  device really cannot be opened, the four-rung ladder cascades on
  `_AUDIO_FAILURE_MARKERS` to mic-only → desktop-only → screen-only,
  and the user sees ffmpeg's actual error in the log (actionable)
  instead of a misleading "(no dshow audio devices)" label.

- **Log sounddevice inputs alongside dshow enumeration** — new
  `[Replay] sounddevice inputs (for cross-debug): [...]` line so an
  operator can compare "what Windows shows" vs "what dshow sees".
  When dshow is empty but sounddevice lists 4 mics + a loopback,
  the fix is always "enable Stereo Mix" or "install VAC", not
  "plug in a mic".

- **`_spawn_health_check(buffer_dir, segment_seconds)`** — background
  one-shot daemon thread started at every successful rung. Waits
  `max(2.0, segment_seconds * 1.5)` seconds then scans
  `buffer_dir/seg*.mp4`:
  - No files → logs `[Replay] HEALTH: No segment files exist after Ns.
    FFmpeg appears alive but the segment muxer has not produced output.`
  - Biggest file < 16 KB → logs `[Replay] HEALTH: First segment is tiny
    (N bytes). FFmpeg is producing near-empty frames -- very likely a
    black/off-screen capture.`
  - Otherwise → `[Replay] HEALTH: First segment OK (N bytes).`
  Non-blocking; never takes down the recorder.

- **Updated startup banner** — `[Replay] Starting buffer: Nm @ Fps WxH
  seg=Ns wrap=N` now reports `eff_w/eff_h` (the size actually passed
  to gdigrab) rather than the raw settings value, so the log matches
  reality when auto-shrink fires.

### Updated FFmpeg command (rung 0 / full audio, after Pass P)

```
ffmpeg -y -hide_banner -loglevel warning \
  -rtbufsize 256M \
  -thread_queue_size 1024 \
  -f gdigrab -draw_mouse 0 \
  -offset_x 0 -offset_y 0 \
  -framerate 30 -video_size 1920x1080 \
  -i desktop \
  -thread_queue_size 512 -f dshow -i audio=<sys_name> \
  -thread_queue_size 512 -f dshow -i audio=<mic_name> \
  -filter_complex "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=0[aout]" \
  -map 0:v -map [aout] \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -f segment -segment_time 5 -segment_wrap 60 -reset_timestamps 1 \
  data/buffer/seg%03d.mp4
```

Differences from Pass M/N/O: added `-thread_queue_size 1024` on the
video side, added explicit `-offset_x 0 -offset_y 0`, and `-video_size`
is now `eff_w×eff_h` (auto-shrunk to the detected desktop when
configured size exceeds it).

### Test checklist (Windows 11 box)

1. **Black-video fix** — set `"width": 9999, "height": 9999` in
   `config/settings.json`, start replay, confirm log shows
   `Capture size 9999x9999 exceeds desktop WxH; shrinking to WxH...`
   and the resulting seg000.mp4 is not black.
2. **Dshow empty-enum** — disable Stereo Mix + no VAC installed.
   Pick a mic in the picker, start replay. Confirm log shows:
   - `FFmpeg enumerated zero dshow audio devices.`
   - `sounddevice inputs (for cross-debug): [...]`
   - At least one `Trying rung ...` line (not straight screen-only).
   - Final rung: `screen-only (all audio rungs failed)` with an actual
     ffmpeg error in the log above it.
3. **Health probe hit** — start replay with the webcam-privacy mic
   blocked by Windows privacy setting. Confirm
   `HEALTH: First segment ...` log fires ~segment_seconds*1.5 after
   rung emit and reports OK / tiny / missing correctly.
4. **Happy path with VAC installed** — install Virtual Audio Capturer,
   start replay, confirm rung emit = `full (desktop + mic)` and
   `HEALTH: First segment OK (N bytes)` where N > 16384.
5. **Resolution detect failure** — uninstall mss briefly, confirm
   `Desktop size probe unavailable (mss missing or failed)` logs and
   capture continues with settings size verbatim (no crash).
6. **Export still works** — after a 2-minute buffer run, trigger
   clip export; confirm concat demuxer path succeeds.

### Known-gap for Pass Q

- True WASAPI loopback capture (via `soundcard` or `-f dshow` against
  a "Virtual Audio Capturer" stub) so desktop audio survives a box
  that has NO dshow audio devices at all. Pass P's removal of the
  hard refuse + the health probe make this failure mode visible and
  correctly labeled, but recovering audio on such a box requires a
  new capture subsystem which is out of Pass P scope.
- GUI-side surfacing of the HEALTH log: an operator today has to watch
  the log pane; a one-line red banner on the Replay tab would make a
  black-clip situation impossible to miss.

### Honest note

As with Passes J → O, Pass P edits were made against the in-repo copy
at `/sessions/modest-zealous-lovelace/mnt/Simian` but have NOT been
executed on Alex's Windows 11 box. Diagnosis is derived from the code
as written (gdigrab + dshow semantics on Win11 7.1.1, plus the Pass M
hard-refuse short-circuit that was removed here). The Test checklist
above is the minimum set Alex needs to run to confirm the fix on his
actual hardware.

---

## Pass O — Global theme system + News/4D regression fix (paste under Pass P)

Root cause of the reported regression: in Pass N we moved `_build_news`
and `_build_4d` behind `self.after(220, ...)` / `self.after(480, ...)`
lazy hydrators, but `_apply_accent_color()` still ran at the end of
`__init__` -- i.e. *before* the lazy builds had created any widgets in
World News / 4D Lab. Result: those tabs ended up with CTk's default
blue palette instead of the user's accent. Pass O fixes that and
generalises the single-color `accent_hex` path into a full global
theme system.

### Hotfix Now (shipped in Pass O)

- **Theme keys in `services/settings_store.py`** — added `theme_bg`,
  `theme_panel`, `theme_accent`, `theme_accent_hover`, `theme_text`,
  `theme_entry`, `theme_log_bg` to `DEFAULT_SETTINGS` with Simian's
  dark / mildly-purple defaults (`#1a1625` bg, `#2a2333` panel,
  `#4da3ff` accent, etc.). Exported `THEME_KEYS` tuple + frozen
  `THEME_DEFAULTS` dict for "Reset to defaults" round-trips.

- **Central `_apply_theme(root_widget=None)` in `gui/simian_gui.py`** —
  resolves the current palette via a new `_current_theme()` helper
  (which bridges legacy `accent_hex` to new `theme_accent`), then
  recursively configures `CTkButton`, `CTkOptionMenu`, `CTkCheckBox`,
  `CTkEntry`, `CTkTextbox`, `CTkScrollableFrame`, `CTkFrame`,
  `CTkLabel` under the root. Also explicitly themes non-CTk surfaces
  that the recursive walk can't reach: the tabview segmented button,
  the 4D Lab `tk.Canvas` bg, and every `link_*` tag on the News
  textbox. `_apply_accent_color` + `_apply_accent_to_widget` kept as
  backwards-compat shims that forward to `_apply_theme`.

- **News / 4D Lab regression fix** — `_lazy_build_news` and
  `_lazy_build_4d` now call `self._apply_theme(self.tab_news)` /
  `self._apply_theme(self.tab_4d)` at the END of the build, after
  the widgets exist. The four hardcoded colors in the old code
  (News link `#6aaeff`, canvas bg `#111111`, SRM point fill
  `#4da3ff`, SRM label `#d0d0d0`) were replaced with
  `self._current_theme()["accent"] / ["log_bg"] / ["accent"] /
  ["text"]` lookups. Hardcoded values retained only as error
  fallbacks inside the try/except.

- **Settings tab theme section** — replaced the single "accent hex"
  entry with seven theme rows. Each row: a label, a hex display, and
  a "Pick" button that opens `tkinter.colorchooser.askcolor`,
  live-applies the chosen color, and mirrors back into
  `self.settings`. Added "Reset to defaults" and "Apply theme now"
  buttons that use `THEME_DEFAULTS` and `save_settings` respectively.

- **Global palette popup + top-bar "Theme" button** — added a small
  top bar above the tabview with a `Theme` button that opens a
  `CTkToplevel` listing every palette slot, sharing the same
  `_pick_theme_color` / `_reset_theme_to_defaults` /
  `_apply_and_save_theme` helpers as the Settings tab. Popup is
  idempotent (re-click raises the existing window) and self-themes
  on open via `_apply_theme(win)`. Layout impact: tabview moves from
  grid row 0 to row 1; grid_rowconfigure updated; top_bar is
  `fg_color="transparent"` so it doesn't introduce a visible
  horizontal band.

- **Live + persistent apply** — `_pick_theme_color` calls
  `self._apply_theme()` immediately for preview. `Apply + Save`
  (in both Settings and popup) calls `save_settings(self.settings)`
  then `_apply_theme()`, so the chosen palette persists across
  restart. `_apply_settings` now writes BOTH the legacy
  `accent_hex` and new `theme_accent` from the accent entry so
  existing consumers of either key keep working.

### Why each change helps

- **Theme keys in settings_store**: single source of truth for colors;
  older widgets that still reach for `accent_hex` see the same value
  as widgets that read `theme_accent`, eliminating the "some tabs
  accented, some not" class of bug.

- **`_apply_theme` over a widget tree walk + non-CTk surfaces**: the
  recursive walk catches any widget created by any future `_build_*`
  method without that method having to know about theming;
  non-CTk surfaces (canvas, text tags) get explicit handling so
  nothing is orphaned.

- **Regression fix**: lazy hydrators now own their own theme pass,
  so News / 4D Lab widgets are themed at the moment they're created
  (not before). The single call to `_apply_theme(self.tab_X)` is
  scoped to just that subtree, so there's no wasted walk over the
  full window.

- **Global palette popup**: theme controls reachable from every tab
  via one click; opens a side window instead of forcing the user
  into Settings -> scroll -> theme frame.

- **Live preview**: each color pick immediately applies, so the user
  sees the result before committing; `Apply + Save` is the commit.

### Acceptance criteria (Pass O)

- [x] `py_compile` passes for `gui/simian_gui.py` and
  `services/settings_store.py` (plus Pass N/M files re-verified).
- [x] AST inspection confirms all nine new methods exist:
  `_current_theme`, `_apply_theme`, `_pick_theme_color`,
  `_reset_theme_to_defaults`, `_apply_and_save_theme`,
  `_open_theme_popup`, plus `_apply_accent_color` and
  `_apply_accent_to_widget` shims and unchanged `_lazy_build_news`
  / `_lazy_build_4d`.
- [x] `THEME_KEYS` and `THEME_DEFAULTS` exported from
  `services.settings_store`; all seven default values are well-formed
  7-char hex codes.
- [x] Grep proves News / 4D Lab call `_apply_theme` at the end of
  their lazy build (two hits at `_apply_theme(self.tab_news)` /
  `_apply_theme(self.tab_4d)` in the lazy builders).
- [x] All four Pass N hardcoded colors in News / 4D Lab replaced
  with `_current_theme()` lookups, with hardcoded hex kept only
  as the except-branch fallback.
- [ ] Runtime smoke (Alex's Windows box): World News "Refresh now"
  / "Clear search" buttons match the global accent. 4D Lab Start /
  Stop buttons and SRM point fill match. Picking a new accent in the
  popup live-updates every tab. Restarting the app preserves the
  picked palette.

### PowerShell compile / grep proof

```powershell
# From D:\Project_C.H.I.M.P\Simian
python -m py_compile gui\simian_gui.py services\settings_store.py services\screen_awareness.py services\replay_buffer.py services\audio_devices.py

Select-String -Path services\settings_store.py -Pattern "THEME_KEYS|THEME_DEFAULTS|theme_bg|theme_panel|theme_accent|theme_text|theme_entry|theme_log_bg"

Select-String -Path gui\simian_gui.py -Pattern "_current_theme|_apply_theme|_pick_theme_color|_reset_theme_to_defaults|_apply_and_save_theme|_open_theme_popup|_theme_pick_labels|_theme_popup|theme_btn|top_bar"

# News/4D Lab regression repaired at lazy-build time
Select-String -Path gui\simian_gui.py -Pattern "_apply_theme\(self\.tab_news\)|_apply_theme\(self\.tab_4d\)"

# Hardcoded colors now only inside except-fallback branches
Select-String -Path gui\simian_gui.py -Pattern "#6aaeff|#111111|#4da3ff|#d0d0d0"
```

### Honest note (Pass O)

- I have **not** runtime-tested Pass O on the real Windows box.
  Compile-check, AST-shape-check, and settings_store import-check
  all pass in the sandbox. The actual visual verification (World
  News / 4D Lab buttons using the accent instead of CTk default
  blue, palette popup live-previewing, persistence across restart)
  needs Alex's next launch.
- Worst-case regressions I can see:
  - If `_apply_theme` raises on an exotic widget subclass, the
    per-widget try/except catches it and themeing continues for
    siblings; only that one widget stays un-themed.
  - If `services.settings_store.THEME_KEYS` fails to import (e.g.
    settings_store was patched mid-flight), `_current_theme` falls
    back to its hard-coded defaults and `_open_theme_popup` skips
    straight to the fallback list -- both paths still work, just
    without the exported constant.
  - `_pick_theme_color` mutates `self.settings` in memory
    immediately but only persists on `Apply + Save`; if the user
    picks colors then quits without saving, they'll see the live
    preview during the session and the old palette next launch.
    That's deliberate (no auto-save on every pick) but may confuse.
- The top-bar button adds ~34 px of vertical space above the
  tabview. I believe this qualifies as a minor addition rather than
  a redesign; if Alex disagrees, the alternative is to attach the
  Theme button to the Logs tab or overlay it with `place()` inside
  the tabview header area -- both are one-liner changes.

---

## Pass N — Performance / choppiness / tab-lag stability

Runtime symptom that triggered this pass: whole app felt laggy during
normal use; switching tabs was visibly slow; 4D Lab still contributed
drag; screen awareness still expensive; hot mic / transcript logging
created constant UI churn. Mandate: make the app feel significantly
smoother WITHOUT changing styling, layout, visual identity, or removing
core features.

### Hotfix Now (shipped in Pass N)

- **UILogger low_resource_mode + self-instrumentation** —
  `gui/simian_gui.py` (`class UILogger`). The log textbox drains via
  `self.root.after(poll_ms, ...)` and used to flush every 120 ms no
  matter the hardware. Added `_current_poll_ms()` which reads
  `low_resource_mode` from settings and returns `max(poll_ms, 250)`
  in that mode, so older boxes redraw the widget half as often while
  throughput stays the same (single flush coalesces all queued lines).
  Added `_perf_drain_total_ms` / `_perf_drain_count` wall-clock
  accounting around the drain body; every 50 drains, if average
  drain took ≥ 4 ms we emit one `[Perf] UILogger flush avg Xms over
  N drains.` line into the queue itself (bounded, so the report
  can't cascade). This makes log-widget hot loops visible without
  a profiler.

- **Hot-mic / transcript backpressure via `_log_dedup`** —
  `gui/simian_gui.py`. Vosk logs a line per partial ("Waiting for more
  speech"), per recognized phrase ("Heard"), and per blocked phrase
  ("Busy, ignored transcript"), which on a talkative user comes in at
  10-20+ lines/second and pins the UI thread even with the bounded
  queue. Wrapped `self._ui_logger.log` with `self._log_dedup`: if a
  message starts with one of four known noisy keys (`[Voice] Heard:`,
  `[Voice] Waiting for more speech`, `[Voice] Busy, ignored
  transcript`, `[Voice] Partial:`) and the same key has been seen
  within a 2-second window, the line is suppressed and a counter is
  incremented. On key-change / window-expiry / non-matching line we
  emit one summary line `<key> (suppressed N similar log lines)` so
  the user still sees evidence of the voice activity. Non-matching
  lines pass straight through with one extra startswith check, so
  normal log traffic is untouched.

- **Lazy tab hydration for News and 4D Lab** — `gui/simian_gui.py`.
  `__init__` used to call `_build_news()` and `_build_4d()`
  synchronously, each doing a full CTk layout pass against the
  offscreen tab before the window even drew. Now they defer via
  `self.after(220, self._lazy_build_news)` and
  `self.after(480, self._lazy_build_4d)` so the first window paint
  isn't blocked by two heavy layout passes. `_news_tab_built` /
  `_fourd_tab_built` flags gate idempotency; `_on_tab_changed`
  force-builds a deferred tab inline if the user clicks faster than
  the scheduled after-id fires, so the user never sees a blank
  tab. `_schedule_news_refresh` now gates on `_news_tab_built` so
  a pre-hydration scheduler tick can't crash on `self.news_category`
  not existing yet. Build latency reported via `[Perf] <tab>
  hydrated in Xms (lazy).` when ≥ 40 ms.

- **Hidden-tab poll throttle for Services** — `gui/simian_gui.py`
  (`_poll_status`). The Services tab is the only place `lbl_api` /
  `lbl_mic` / `badge_api` / `badge_mic` are visible, but the 1.8 s
  reschedule ran every 1.8 s regardless of current tab. Added a
  `visible = str(self.tabs.get()) == "Services"` probe and rescheduled
  at `1800 ms` when visible vs `6000 ms` when hidden (3x reduction in
  port probes + Vosk path lookups + label configure calls). Under
  `low_resource_mode` the cadence shifts to `3000 ms` visible /
  `12000 ms` hidden. Label configure calls wrapped in try/except so
  a transient Tk teardown during close doesn't raise.

- **News refresh floor under low_resource_mode** —
  `gui/simian_gui.py` (`_schedule_news_refresh`). Raised the minimum
  refresh interval from `max(30, news_refresh_seconds)` to
  `max(180, news_refresh_seconds)` when `low_resource_mode` is on.
  Older boxes no longer hammer RSS sources + HTML parser every
  minute.

- **Screen awareness in-flight guard** — `services/screen_awareness.py`.
  Overlapping `capture_now` calls (user hits the mic button twice
  while a 90 s vision request is still in flight) used to each spawn
  another Ollama round trip, each competing for the same VRAM budget.
  Added a module-level `_CAPTURE_INFLIGHT_LOCK = threading.Lock()`
  and split `capture_now` into a guarded public wrapper plus
  `_capture_now_inner` body. Wrapper does a non-blocking
  `acquire(blocking=False)`; when the lock is held we fast-fail with
  a `ScreenSnapshot(degraded_reason="busy_prior_capture_inflight")`
  and log one line so the user knows why the second call returned
  fast. `finally` releases the lock even if the inner body raised.

- **Startup perf marker in `_auto_start`** — `gui/simian_gui.py`.
  `self._startup_t0 = time.perf_counter()` is captured at the end of
  `SimianApp.__init__` (right before the lazy-build schedule). When
  `_auto_start` fires we emit `[Perf] Startup reached _auto_start at
  +Xms.` so any future regression in the startup_delay path shows
  up on every launch log.

- **SRM tick (unchanged from Pass M, still honored)** —
  `low_resource_mode` keeps emitting `push_every=4` / 66 ms interval
  (~15 Hz render, ~7.5 Hz telemetry); fast path when the mode is
  off remains 33 ms / every tick.

### Why each change helps performance

- **UILogger poll slowdown + drain instrumentation**: one fewer
  wake-up every 130 ms on older hardware, and the self-reported
  `[Perf]` line gives us an in-line profile of how expensive the
  textbox insert + trim actually is. Flush throughput is preserved
  because a single flush still drains up to `batch_size` items.

- **Voice-log dedupe**: hot-mic talkative sessions stop firing the
  textbox insert path 10-20 times per second. The dedupe check is a
  `startswith` scan over 4 literals + one wall-clock compare — cheap
  enough to leave on for normal traffic too.

- **Lazy hydration**: two heavy CTk layout passes move out of the
  critical path between `__init__` and first paint. Users see Chat
  immediately; News / 4D Lab materialize while the app is already
  interactive. First-visit force-build means the user never looks at
  an empty tab.

- **Hidden-tab poll throttle**: `port_in_use` is a non-trivial socket
  probe, and `_resolve_vosk_model_dir` does a filesystem walk. Cutting
  their frequency 3x when the Services tab isn't visible removes a
  steady background cost without degrading the UX (labels are only
  visible when the user is on that tab anyway).

- **News refresh floor**: fetching + parsing 60 RSS items with
  `httpx` + `html.parser` every 60 s on an older CPU was pinning
  a worker thread. 180 s floor is still plenty fresh for a news
  wall.

- **Screen awareness in-flight guard**: double-fires no longer
  queue up a second 90 s Ollama request while the first is still
  running. Second call returns immediately with a busy flag so the
  caller can either retry later or render the degraded snapshot.

- **Startup marker**: tells you, on every launch, where the
  wall-clock went between `__init__` and the staged-start kickoff.
  Regressions stand out in the log without needing a profiler.

### Acceptance criteria (Pass N)

- [x] `py_compile` passes for `gui/simian_gui.py`,
  `services/screen_awareness.py`, `services/replay_buffer.py`,
  `services/audio_devices.py` in this pass.
- [x] Grep proves `_log_dedup`, `_lazy_build_news`, `_lazy_build_4d`,
  `_CAPTURE_INFLIGHT_LOCK`, hidden-tab throttle, news floor, and
  startup perf marker all exist in the live repo.
- [ ] Runtime smoke (Windows, Alex's box): hot-mic session that
  previously produced `[Voice] Heard:` 10+ times in a row now
  produces at most one line + one `(suppressed N similar log lines)`
  per 2 s window.
- [ ] Runtime smoke: two "what's on my screen" mic commands fired
  inside one vision round-trip return one real snapshot and one
  `busy_prior_capture_inflight` degraded snapshot.
- [ ] Runtime smoke: launching the app logs `[Perf] Startup reached
  _auto_start at +Xms.` with X < 5000 on a warm box.
- [ ] Runtime smoke: switching tabs between Chat / News / 4D Lab is
  visibly smoother; the first switch to a deferred tab logs
  `[Perf] <tab> hydrated in Xms (lazy).`.

### Honest note (Pass N)

- I have **not** runtime-tested any of these edits on the real
  Windows box. I compiled the files and grep-proved every
  edit; runtime behavior is what Alex's next Windows session will
  reveal. If `_log_dedup` misbehaves on a key I didn't anticipate,
  the worst case is that noisy lines fall through uncollapsed
  (identical to the pre-Pass-N behavior). If `_lazy_build_news`
  raises during the deferred build, `_news_tab_built` stays `False`
  and the scheduled refresh loop will keep no-op'ing -- the
  first-visit force-build path will then try again the next time
  the user clicks the News tab.
- The hidden-tab `_poll_status` throttle assumes the user's default
  tab isn't Services; if they pin Services as their working tab
  the visible cadence wins and this change is a no-op for them
  (correct behavior, but no perf win either).
- The in-flight guard only protects against concurrent `capture_now`
  calls *inside the same process*. Two GUI instances would each
  have their own `_CAPTURE_INFLIGHT_LOCK` and would still race
  Ollama. That's a theoretical problem only.
- Pass M's four-rung audio fallback ladder and VAC preference are
  untouched; Pass N did not modify `services/audio_devices.py` or
  `services/replay_buffer.py` beyond what Pass M already shipped.

---

## Pass M — Windows audio capture + vision OOM + hardware-scaling

### Hotfix Now (shipped in Pass M)

- **Refuse doomed audio rungs when dshow enum is empty** —
  `services/replay_buffer.py`. The latest Windows run showed FFmpeg
  enumerating zero dshow audio devices and the recorder still trying
  `audio=Stereo Mix ...` / `audio=Microphone Array ...` names
  that were guaranteed to fail. `_audio_input_args()` now tracks an
  `enum_ok` flag, and when the enumeration call returns cleanly with
  zero devices it refuses both sys and mic inputs up front, emits
  one actionable hint (enable Stereo Mix / install a Virtual Audio
  Capturer filter / grant mic access in Windows Privacy settings),
  and lets the ladder emit `screen-only (no dshow audio devices)`
  so the rung label tells the truth. Fallback ladder otherwise
  unchanged.

- **Prefer Virtual Audio Capturer in auto-pick** —
  `services/audio_devices.py`. Added `VAC_HINT_RE`. VAC (and
  VB-Audio / Voicemeeter) capture the default playback mix without
  needing Stereo Mix enabled, so `pick_best_system_audio_choice()`
  now checks VAC first, then the legacy loopback/Stereo Mix
  pattern, then falls back to the WASAPI sentinel. Nothing is
  removed from the picker -- this just changes *which* available
  name is preferred when multiple are present.

- **Trademark-glyph-insensitive dshow name resolver** —
  `services/replay_buffer.py`. `_normalize_for_match()` strips
  Unicode `\u00ae` / `\u2122` / `\u00a9`, their `(R)` / `(TM)` /
  `(C)` ASCII approximations, NFKD-folds accents, and collapses
  whitespace. `_resolve_dshow_audio_name()` falls through to this
  normalized contains/equals match as a final rung, catching the
  real-world drift between Windows Sound panel (`Intel\u00ae`) and
  FFmpeg enumeration (`Intel(R)`).

- **Session-level OOM blacklist for vision models** —
  `services/screen_awareness.py`. `_OOM_MODELS` + `_mark_model_oom()`
  / `_model_is_oom()`. Once a primary vision model returns
  `memory_alloc` on this session (confirmed memory pressure on
  qwen3-vl:8b-thinking from the user's box), subsequent calls skip
  it and route straight to the configured lighter model (or skip
  vision entirely if none is configured). Clears on process
  restart so driver/VRAM changes don't need a code edit. Net
  effect: no more 180s + 180s + fallback wait on every single turn
  once the heavy model has been proven to OOM.

- **SRM tick throttle for older hardware** —
  `gui/simian_gui.py`. When `low_resource_mode` is true, the 4D
  Lab SRM visualizer reschedules itself every 66 ms (≈15 Hz) and
  pushes telemetry only every 4th tick. At default resource mode
  the 33 ms / 30 Hz cadence is unchanged, so the only behavior
  change is under the explicit older-hardware switch.

### Stabilization Next (Pass N candidates)

- **Honest post-launch audio verification** — add an opt-in
  `ffprobe` sweep of the newest completed replay segment once per
  minute; if a rung claims full-audio but the mp4 has no audio
  stream, step the rung down to `screen-only (audio silently
  dropped)` and surface the fact on the 4D Lab pill. Honest
  reporting > silent success.

- **Ship a bundled Virtual Audio Capturer installer helper** —
  one-shot Settings action that downloads + explains the rdp
  VAC filter install and re-enumerates dshow devices. Reduces
  "enable Stereo Mix in the Sound panel" support burden on
  non-technical users.

- **WASAPI loopback direct capture** — FFmpeg mainline has only
  dshow on Windows; evaluate a tiny C helper or a PowerShell
  path that uses the WASAPI loopback API to pipe desktop audio
  to ffmpeg via stdin. Bigger surface than Pass M should touch;
  keep it scoped.

- **Honest microphone validation** — probe the selected mic with
  a 0.5 s silent capture and report back through the health
  badge whether the named device actually opened and produced
  samples. Reuses the existing enum path; the win is exposing
  "opened but zero frames" cases that look fine in logs.

- **Cross-hardware stability profiles** — ship a small YAML
  profile loader so `low_resource_mode` is one knob in a
  broader set (replay bitrate, vision max_dim, vision timeout
  floor, SRM interval, news refresh rate). Lets us pre-ship
  sensible profiles for "8-year-old laptop" / "new RTX box"
  without per-setting edits.

- **Vision health pill** — render the last 60 s of
  `vision:ok` / `vision:fail` / `oom_blacklist` events as a
  sparkline pill in 4D Lab so the user can see at a glance
  whether the current model is viable on this box.

- **Hidden-tab clock freeze** — audit every `self.after(...)`
  scheduler and wrap the ones that trigger UI redraws with an
  `if self.tabs.get() == <owner_tab>` guard. Pass L did SRM +
  News; replay status, clip list refresh, and settings pulse
  are next.

### Feature Scaffolds (hooks exist, surface bigger work)

- **Replay audio reliability across Windows hardware** —
  document and test against at least four hardware profiles
  (Stereo Mix on, Stereo Mix disabled + VAC installed, VAC-
  only, headset + array mic). Ship a matrix in the Help tab.

- **Microphone-source validation / exact-name mapping** —
  graduate `_normalize_for_match` into a public helper and
  reuse it in any other place that resolves dshow names
  (future STT pipeline, future screen-record-with-mic flow).

- **Vision lightweight-model routing policy** — richer policy
  than "if heavy model fails, try lighter". Consider model
  health memory per model, not just per session; promote a
  lighter model to primary after N session OOMs.

- **Always-on telemetry feed** — make `four_d_telemetry`
  subscribable from any tab so the 4D Lab ring isn't the only
  consumer. Status bar, health panel, and chat mentions can
  all listen.

### ML Foundations (no new work this pass, still open)

- Supervised learning integration, RL loop scaffold,
  preprocessing utilities, local training wrapper, metrics
  store, embeddings corpus, predictive automation hooks.

### Sprint plan (unchanged)

World Tracker -> 4D Lab polish -> Files -> Generative. Gated on
Stabilization Next row landing first.

---

## Pass L — Action scaffolds (paste-at-top kept for history)

### Hotfix Now (shipped in Pass L)

- **Replay rung telemetry publish** — `services/replay_buffer.py`.
  The `[Replay] Capture rung: <name>.` summary line now also
  publishes a `replay:rung` event on the 4D Lab telemetry ring, so
  downstream widgets can watch live capture state without polling
  ffmpeg or the process table.
- **Vision outcome telemetry publish** — `gui/simian_gui.py`. Every
  screen-query response publishes a `vision:ok` (with chars + model)
  or `vision:fail` (with reason + model) event. Feeds the Lab
  rolling "last vision call" pill.
- **TTS start/done telemetry publish** — `gui/simian_gui.py`. Every
  long-chunk TTS run emits `tts:start` and `tts:done` events
  bracketed around the existing chunk ladder. The TTS lock and
  sequential playback stay unchanged -- these are pure observers.
- **low_resource_mode setting** — `services/settings_store.py` +
  `services/screen_awareness.py`. New boolean key (default False).
  When true, `capture_now()` clamps `max_dim` to 800 px and
  `timeout` to 90 s as a safety net for weaker machines. User's
  explicit per-call values win if they're already tighter.
- **Crash-audit / health-scan scaffold** —
  `services/health_audit.py`. Bounded probe suite (python,
  settings, mss+PIL, ffmpeg, dshow audio, sounddevice, ollama,
  buffer_dir writable, four_d_telemetry importable). One-shot
  non-destructive self-test; each probe never raises. Ready for a
  Settings-tab "Run health audit" button and a CLI entry point
  (`python -m services.health_audit`).

### Stabilization Next (Pass M candidates)

- **Wire health_audit into the Settings tab** — a "Run health
  audit" button + modal that renders the state table and exports
  the JSON to a file. Same probes, new surface.
- **WASAPI loopback desktop-audio path** — when `dshow` audio
  enumeration is empty, bridge via sounddevice WASAPI loopback into
  a named pipe that FFmpeg reads. Replaces the "Stereo Mix is off,
  you get screen-only" dead-end on modern Windows.
- **Replay clip autoverify (luma sampler)** — after each segment
  rotates, sample 3 frames and tag black-frame-suspected clips in
  the Clips UI. Reuses the existing luma probe helper.
- **Honest replay health badge** — badge shows the actual rung
  ("running (full)", "running (mic only)", "running (desktop only)",
  "running (screen only)", "black-frame suspected"), driven by the
  `replay:rung` telemetry event.
- **Vision health ring** — subscribe to `vision:*` events and
  keep a rolling latency/outcome table so the Logs tab can answer
  "is vision slow or failing?" without grep.
- **Settings hot-reload signal** — most settings take effect on
  next launch. Wire an observable so keys like `low_resource_mode`,
  `screen_awareness_retry_budget_factor`, and
  `screen_awareness_lighter_vision_model` apply live.
- **UI-log cumulative rate-limiter** — per-key hash + suppressor in
  `UILogger` to swallow duplicate lines (>99% of replay ffmpeg
  warnings, for example).

### Feature Scaffolds (ready to flesh out)

- **Crash-audit hook** — on every startup, quietly run
  `run_health_audit()` and log a one-line summary
  ("[Audit] 7 ok / 1 degraded / 0 error"); on first app crash,
  dump the full audit table into a crash file. Foundation for
  "proactive fix-every-feature-dependency" behavior the user asked
  for.
- **Point Monkey facts (4D Lab always-on low-res)** — tiny ring of
  "facts" derived from telemetry events (e.g. "replay: mic-only",
  "last TTS: 12s", "vision: ok x3"). Renders as a fixed 100x200
  corner overlay at ~1 fps while 4D is visible. Tied to
  `four_d_telemetry.snapshot()` so it never polls. Respects
  pause-when-hidden.
- **Audio Everywhere panel** — single Services sub-tab that shows
  every audio route: STT input, TTS output, Replay desktop audio,
  Replay mic. Honest labels for "real dshow device" vs
  "auto-picked WASAPI loopback" vs "placeholder".
- **Chat true drag-and-drop** — wire `tkinterdnd2` (optional import,
  fall back to Ctrl+O scaffold that shipped in Pass K). DnD handler
  reuses `_attach_file_for_chat` so both paths share validation.
- **Files → drag into chat** — Files-tab row drag to chat entry;
  on drop, same `_selected_file_context` pipe.
- **World Tracker v1** — promote World News to World Tracker:
  News (existing), Weather (OpenWeather + local cache), Markets
  (Alpha Vantage). Reuses the Pass K pause-when-hidden policy.
- **World Tracker map** — add a Leaflet/Folium static map view with
  region pins sourced from news geotags. Click-through opens the
  associated article in the existing news pane. Scaffold only.
- **Generative in-chat palette** — a `/generate <prompt>` chat
  command that routes to `services/image_gen.py` or
  `services/video_gen.py` and returns an inline preview path.
  No new tab; lives in the existing chat surface.

### ML Foundations (capture only, do not overbuild)

- **Supervised learning hooks** — thumbs-up/down on chat responses;
  export JSONL for offline training.
- **Reinforcement learning loop** — reward = thumbs / re-ask /
  follow-up rate; offline aggregator first, online later.
- **Data preprocessing tools** — cleaner + dedupe + PII scrub over
  the exported JSONL.
- **Model training wrapper** — one-stop dispatcher that calls
  `llama.cpp` fine-tune or TRL + LoRA depending on the target
  model family.
- **Metrics / evaluation tracking** — SQLite store + a Metrics tab
  that plots latency / success-rate trends per subsystem. Health
  audit rows feed into it.
- **Embeddings + retrieval** — embedding index over chat history +
  Files + recent clips, backed by `embeddinggemma:300m`.
- **Predictive automation** — recurring-intent detector with opt-in
  proactive nudges ("every Mon 9am you open the Files tab").

### World Tracker / 4D / Files / Generative sprint plan

- **Sprint 1 (1 week):** World Tracker MVP (news + weather);
  Files directory scan + metadata table; Settings-tab health-audit
  button.
- **Sprint 2 (1 week):** 4D Lab Point Monkey overlay; Lorenz /
  double pendulum physics presets; Files search + preview.
- **Sprint 3 (2 weeks):** Generative in-chat palette; Files
  embedding index + semantic search; cross-tab "open in 4D" action.
- **Sprint 4 (2 weeks):** Unified command palette (chat + files +
  clips + generative); replay-buffer → generative "stylize this
  clip"; World Tracker map view; Audio Everywhere panel.

---

## Pass K — Action scaffolds (latest, paste at top of Flourishin)

### Hotfix Now (shipped in Pass K)

- **Four-rung replay fallback ladder** — `services/replay_buffer.py`.
  Explicit rung order now: `full (desktop + mic) → mic-only →
  desktop-only → screen-only`. Each rung logs its own attempt and the
  winning rung emits a single `[Replay] Capture rung: <rung>.`
  summary line. Prior ladder went straight from full → mic-only →
  screen-only, which meant "mic broken, desktop audio healthy" always
  collapsed to silent screen-only.
- **Vision OOM / memory classification** —
  `services/screen_awareness.py` + `gui/simian_gui.py`. Ollama 500
  bodies containing "memory" + "alloc"/"out of"/"layout" now surface
  as `memory_alloc:<body>` (with its own user-facing message pointing
  to `screen_awareness_lighter_vision_model`). Separate
  `model_load_failure:<body>` code for "model load failed" shapes.
- **Aggressive downscale on memory-alloc retry** —
  `services/screen_awareness.py`. When attempt 1 fails with
  `memory_alloc`, retry downscales the PNG to a 640 px cap and
  skips the warmup ping (the issue wasn't cold start). Keeps the
  warmup+retry path for `timeout`/`empty_response`.
- **News refresh pause-when-hidden** — `gui/simian_gui.py`. Scheduled
  news refresh now skips the actual fetch when the World News tab
  is hidden, and `_on_tab_changed` triggers a fresh fetch if the
  cache is stale when the user returns. No wasted network or UI
  work while the tab is invisible.
- **4D Lab telemetry service scaffold** —
  `services/four_d_telemetry.py`. New bounded-deque pub/sub (cap 256
  events). Chat turns now publish `chat:user_turn` events. Ready for
  TTS/vision/replay/mic sites to fan-in without any hot-path cost.
  GUI overlay in 4D Lab is backlog ("live overlay" task below).
- **Chat file-attach keyboard scaffold** — `gui/simian_gui.py`.
  Ctrl+O on the chat entry opens a file dialog and loads the file
  into `_selected_file_context` (same channel the Files tab uses).
  True drag-and-drop onto the chat input is backlog.

### Stabilization Next (Pass L candidates)

- **WASAPI loopback desktop-audio path** — when `dshow` enumeration
  returns zero Stereo-Mix-like devices, bridge via sounddevice's
  WASAPI loopback into a named pipe that FFmpeg reads. Big but the
  right answer for modern Windows where Stereo Mix is off by default.
- **Replay clip autoverify (luma sampler)** — after every 3rd segment
  finishes, sample 3 frames and tag black-frame-suspected clips in
  the Clips UI. Uses the existing luma probe helper.
- **Honest replay health badge** — badge shows "running (full)",
  "running (mic only)", "running (screen only)", "black-frame suspected",
  not just "running".
- **Vision health ring buffer** — last 10 vision calls with model,
  duration, outcome, reason. Lets the user distinguish "slow but fine"
  from "chronically failing".
- **Settings hot-reload signal** — most settings today take effect
  on next launch. Wire `reload_settings` event so keys like
  `screen_awareness_retry_budget_factor` and
  `screen_awareness_lighter_vision_model` apply live.
- **Cumulative log rate-limiter** — best-guess, ffmpeg, and vision
  logs still get chatty. One "per-key hash + suppressor" helper in
  `UILogger` can swallow >99% of duplicate lines.

### Feature Scaffolds (next deliverables)

- **Chat drag-and-drop** — wire `TkinterDnD2` (optional import, fall
  back to Ctrl+O scaffold shipped this pass). DnD handler should
  reuse `_attach_file_for_chat`'s logic path.
- **Files → drag into chat** — Files-tab row drag to the chat
  entry area; on drop, same `_selected_file_context` pipe.
- **Audio everywhere panel** — single Services sub-tab that shows
  every audio route: STT input, TTS output, Replay desktop audio,
  Replay mic. Ties into `audio_devices.py` helpers.
- **World News → World Tracker** — promote tab to "World Tracker"
  with three columns: News (existing), Weather (new, OpenWeather +
  local cache), Markets (Alpha Vantage / Yahoo scrape). Reuses the
  pause-when-hidden policy shipped this pass.
- **4D Lab live telemetry overlay** — consume
  `services.four_d_telemetry.telemetry.snapshot()` from the SRM
  tick; render event bullets along the right edge without touching
  the main sinusoid path. Respect pause-when-hidden.
- **4D Lab real physics** — swap synthetic sinusoids for Lorenz /
  double pendulum integrators with a dropdown. Telemetry schema
  stays stable so downstream analyzers don't break.
- **Generative tools in-app** — local SD / FLUX endpoint via
  `services/image_gen.py` (already stubbed) and
  `services/video_gen.py`. Surface as a "Make" palette item inside
  the chat rather than a new tab.

### ML Foundations (capture only, do not overbuild)

- **Supervised learning hooks** — record input/output pairs with
  user thumbs-up/down; export JSONL for offline training.
- **Reinforcement learning loop** — tie reward signal to explicit
  user actions (thumbs, re-ask rate, follow-up within 30s). Offline
  aggregator first; online later.
- **Data preprocessing tools** — cleaner/normalizer library for
  the JSONL export (PII scrub, dedupe, split).
- **Model training libraries / hooks** — one-stop wrapper that
  dispatches to llama.cpp fine-tune or TRL + LoRA depending on
  target model family. Not in-repo initially; just a scaffold.
- **Performance metrics + visualization** — on-disk metrics store
  (SQLite) + a Metrics tab that renders latency / success-rate
  trends per subsystem.
- **Better contextual understanding** — embedding index over chat
  history + Files + recent clips. Gated by `embeddinggemma:300m`.
- **Predictive support / automation** — recurring-intent detector
  ("every Monday 9am you open the Files tab") with opt-in
  proactive nudges.

### World Tracker / 4D / Files / Generative sprint plan

- **Sprint 1 (1 week):** World Tracker MVP — news + weather panels;
  Files tab directory scan + metadata table.
- **Sprint 2 (1 week):** 4D Lab Lorenz + double pendulum; Files tab
  search + preview; 4D live telemetry overlay.
- **Sprint 3 (2 weeks):** Generative local SD endpoint; Files tab
  embedding index + semantic search; cross-tab "open in 4D" action
  from a Files preview.
- **Sprint 4 (2 weeks):** Unified command palette (chat + files +
  clips + generative); replay-buffer → generative "stylize this
  clip" one-click; Audio Everywhere panel.

---

## Pass J — Restructured action scaffolds (paste into Flourishin)

These are the concrete work items grouped by when they should happen,
rather than by priority tier. The items below the fold (P0–P4, ML
roadmap, intent recovery, appendices) stay authoritative for deep
context; this top section is the "what ships when" view.

### Hotfix Tonight (shipped in Pass J)

- **gdigrab-draw-mouse-off**
  - file: `services/replay_buffer.py`
  - change: added `-draw_mouse 0` to the gdigrab input args
  - why: Windows logs showed `Could not get cursor info (error 5)` +
    `Failed to capture image (error 5)` on UAC-protected foreground
    windows, which degraded the clip to a black frame with only the
    cursor ghost. Disabling cursor capture sidesteps the ACCESS_DENIED
    query entirely.
  - acceptance: no `error 5` lines in the replay ffmpeg output; clip
    visibly shows desktop contents on the next start.

- **dshow-audio-device-validation**
  - file: `services/replay_buffer.py` (+ uses existing
    `services/audio_devices.py::list_dshow_audio_devices`)
  - change: new `_resolve_dshow_audio_name(requested, available)`
    helper + enumeration of dshow audio devices before handing names
    to ffmpeg; logs the available list and maps picker → actual name
    or skips that input with an actionable log line.
  - why: runtime log showed `Could not find audio only device with
    name [Stereo Mix ...] among source devices of type audio`, which
    means the picker's Windows-Sound name didn't match ffmpeg's
    dshow enumeration exactly. Fail soft and be honest about it.
  - acceptance: ffmpeg output no longer contains `Could not find
    audio only device`. On a mismatch the replay log names the exact
    available devices so the user can pick the right one.

- **mic-only step-down fallback**
  - file: `services/replay_buffer.py`
  - change: on audio-pipeline failure, try mic-only (drop system
    audio, keep mic) before falling all the way back to screen-only.
  - why: when Stereo Mix is broken but the user's mic is healthy
    we shouldn't silently drop ALL audio.
  - acceptance: when the first attempt fails and a mic is
    configured, the logs show `Running in mic-only mode (no system
    audio).` and the resulting clip has voice-over audio.

- **vision retry budget tunable + lighter-model fallback**
  - files: `services/settings_store.py`, `services/screen_awareness.py`
  - change: new `screen_awareness_retry_budget_factor` (default
    `1.0`, was implicit `1.5`) and `screen_awareness_lighter_vision_model`
    (default empty). Retry no longer stretches to 270s on a 180s
    base. Optional lighter model is tried as a final fallback on
    timeout/empty_response.
  - why: runtime evidence showed `Vision retry... timeout=270.0s`
    after an initial 180s timeout, i.e. up to 450s of blocking user
    time before a definitive answer.
  - acceptance: with defaults, worst-case vision wait = `base + warmup
    + base` (≈ 180 + 30 + 180 = 390s); with `factor=0.5` it's ≈ 300s.
    Lighter model is tried automatically when configured.

- **multi-window vision prompt enrichment**
  - file: `services/screen_awareness.py`
  - change: the vision prompt now includes a bulleted list of
    visible window titles so the model knows about overlapping apps
    even when only one shows clearly in the rasterized frame.
  - acceptance: Logs tab shows the enriched prompt includes
    `Context: the user currently has these windows open...`, and
    multi-window answers reference the other apps by name.

- **SRM pause-when-hidden**
  - file: `gui/simian_gui.py`
  - change: `CTkTabview` now has a `command=self._on_tab_changed`
    hook; `_srm_tick` stops rescheduling entirely when 4D Lab is not
    the active tab; `_on_tab_changed` restarts it when the user
    comes back to 4D Lab.
  - why: prior throttle still cost a 250ms redraw + telemetry post
    while hidden. Now 0ms while hidden.
  - acceptance: on the Logs tab, SRM telemetry POSTs stop entirely
    while another tab is active, and resume within ~1 tick of
    switching back to 4D Lab.

### Next Stability Pass (Pass K candidates)

- **Preflight diagnostics panel** — Settings button that runs
  `ffmpeg -list_devices`, reports Ollama `/api/tags`, and surfaces
  mss capture ok/fail. One screen, three green/amber lights.
- **Honest replay health badge** — today "running" means ffmpeg is
  alive; extend to "running (screen-only)", "running (no audio)",
  "running (black-frame suspected)", "degraded".
- **Clip autoverify** — sample 3 frames from the newest seg file; if
  mean luma of all 3 < 3.0, tag the clip as black-frame-suspected.
- **Vision health table** — last 10 vision calls with duration,
  model, outcome; lets user tell "slow but fine" from "timing out."
- **Settings hot-reload** — current settings only take effect after
  restart for some fields (retry budget factor included). Wire a
  `reload_settings` signal so most keys update live.

### Feature Scaffolds (ready to flesh out)

- **World Tracker tab** — headline feed + city weather + market
  snapshot. Already stubbed; wire to existing news fetcher +
  OpenWeather + Alpha Vantage (or a local cache).
- **4D Lab — real physics** — move SRM from synthetic sinusoids to
  a proper integrator (Lorenz, double pendulum). Keep telemetry
  schema so downstream analyzers don't break.
- **Files tab — project memory** — recursive scan + embedding
  index of the user's working folder (opt-in, per-folder). Powers
  "what was I working on yesterday?"
- **Generative — image + short-video pipelines** — wire an SD /
  FLUX comfyui-style local endpoint; expose a single "Make" button
  with style presets.

### ML Foundations

- **Embedding backend choice** — lock to `embeddinggemma:300m` or
  `nomic-embed-text:v1.5` for file memory + intent recall. Benchmark
  quality/latency.
- **Intent recall with recency decay** — 7-day sliding window,
  exponential decay, surface "you were working on X" on launch.
- **Voice identity clustering** — cluster mic-listener utterances
  per-speaker; opt-in, never leaves disk.
- **Vision classification head** — small ONNX model that gates the
  full vision call (is this even a window we care about? screensaver?
  game fullscreen?). Avoids the 180s timeout when the user is in-game.

### UX Performance

- **Log viewer virtualization** — the Logs tab re-renders the whole
  Textbox on append; swap to `after_idle` batching + trim to last N
  lines. Already 700-cap, but the cap is soft.
- **Replay buffer disk-watcher** — segment list UI should push on
  filesystem events, not poll every 2s.
- **Dark-mode audit** — pink accent is consistent, but the audio
  picker toplevel + a few CTkDialogs still have white chrome in
  certain states.
- **Tab-switch profiler** — record tab-switch latency so we can
  see if anything else costs 50ms+ like SRM used to.

### World Tracker / 4D / Files / Generative sequencing

- **Sprint 1 (1 week):** World Tracker live feed (news + weather);
  Files tab basic directory scan + metadata table.
- **Sprint 2 (1 week):** 4D Lab Lorenz attractor + double pendulum
  physics presets; Files tab search + preview.
- **Sprint 3 (2 weeks):** Generative local SD endpoint; Files tab
  embedding index + semantic search; cross-tab "open in 4D" action
  from a Files preview.
- **Sprint 4 (2 weeks):** Unified command palette over chat + files
  + clips + generative; replay-buffer → generative "stylize this
  clip" one-click.

---

## P0 — Runtime-critical (ship first)

### Replay buffer reliability — no more black-screen clips

- **priority:** P0
- **category:** replay / capture
- **description:** Replay sometimes records a black screen with only the
  cursor (see `clip_20260423_084641.mp4`), while a later clip captured
  on the same session is valid (`clip_20260423_091641.mp4`). Treat this
  as an init-order / desktop-composition-not-ready regression, not a
  total failure.
- **acceptance criteria:**
  - Replay never marks itself healthy when the first captured frames
    are near-black.
  - A desktop-readiness probe runs before FFmpeg starts; re-arms once
    if the desktop surface is not yet composed.
  - Screen-only fallback for failed audio capture remains in place.
  - No regression on the manual Start Buffer / Stop Buffer controls.

### Replay desktop audio + microphone — actually honor the picker

- **priority:** P0
- **category:** replay / audio
- **description:** Picker now shows the right desktop/mic choices
  including Stereo Mix, but real clips still don't reliably include
  both streams. Need the chosen devices to actually reach FFmpeg, with
  honest fallback + log lines when a device can't be opened.
- **acceptance criteria:**
  - Desktop audio spec passed to FFmpeg matches the picker selection
    exactly (no truncation / no silent drop).
  - Mic spec passed to FFmpeg matches the picker selection exactly.
  - If either device can't be opened, the GUI log shows the exact FFmpeg
    error and the reason.
  - Persisted settings are used on next restart without re-picking.

### Replay delayed autostart — stop choking app launch

- **priority:** P0
- **category:** replay / startup
- **description:** Replay must not start immediately at app boot. It
  should auto-start about 45 s later on the main thread, cancelable on
  close. Manual start before the timer fires must not double-start.
- **acceptance criteria:**
  - Delay is configurable via `replay_autostart_delay_ms` (default
    45 000).
  - Log line at scheduling, at firing, and at skip (already-running /
    disabled / user-started-manually).
  - Cancelled cleanly when the app is closed during the wait window.
  - Enabled by default so the out-of-box experience is "it records."

### Screen awareness — faster + richer fallback

- **priority:** P0
- **category:** screen awareness / ollama
- **description:** Vision calls still hit timeout, empty_response, and
  http_500 "memory layout cannot be allocated" when the vision model
  can't load on the current VRAM budget. When that happens the user
  still deserves a concrete answer: at minimum active window + other
  visible windows, and a specific "switch to a lighter model" hint.
- **acceptance criteria:**
  - `http_5xx` is treated as fatal (no retry) so we don't waste a full
    timeout budget on a model that can't load.
  - Capture-only fallback emits `active window`, `active app`, and a
    short list of other visible window titles when vision fails.
  - Self-window note stays intact when only Simian is on screen.
  - "Look at / on my screen" phrasing still routes to the screen query.
  - The http_500 message points the user at a lighter vision model.

### Chat-path timeout resilience

- **priority:** P0
- **category:** chat / ollama
- **description:** Normal chat still times out sometimes on cold
  starts. Retry with warmup ping so the second attempt doesn't pay the
  cold-load cost again. Don't slow the working happy path.
- **acceptance criteria:**
  - On `httpx.TimeoutException`, fire a cheap `/api/generate` warmup
    with `keep_alive: 5m` before retrying.
  - Honest error surfacing when both attempts fail.
  - Phase-split `httpx.Timeout` on every call so connect / write
    phases don't eat the long read budget.
  - No TTS regressions; chunked rollover still works.

### Settings load resilience — no silent config loss

- **priority:** P0
- **category:** settings / durability
- **description:** A truncated / corrupt `config/settings.json` used to
  silently fall back to defaults with no visible reason, so the user
  saw defaults even though they had customised settings. Surface the
  parse failure on stderr, and tolerate UTF-8 BOMs on read.
- **acceptance criteria:**
  - `load_settings` reads with `utf-8-sig` so a stray BOM doesn't break
    parsing.
  - Parse failures print a single line to stderr naming the error.
  - Existing per-key default-fill behavior preserved.

### UI stutter — tab switching + 4D Lab

- **priority:** P0
- **category:** ui / performance
- **description:** Tabs feel choppy, and starting the 4D Lab SRM
  visualizer makes it worse. The Tk main thread was redrawing the
  canvas + pushing telemetry at 30 fps even when the user was on
  another tab.
- **acceptance criteria:**
  - SRM canvas redraw is skipped and tick cadence drops to ~4 Hz when
    the 4D Lab tab is not the active tab.
  - Telemetry POST is suppressed while the tab is hidden.
  - SRM state advancement continues (theta/phi/sigma stay monotonic).
  - No visible lag introduced on the active tab.

---

## P1 — Near-term UX

### Drag-and-drop file into Chat

- **priority:** P1
- **category:** chat / files
- **description:** Users want to drop a file onto the Chat tab and have
  it attached + summarised into the next prompt without going through
  the Files tab.
- **acceptance criteria:**
  - `tkinterdnd2` drop target on the Chat frame (optional dep; graceful
    degrade when absent).
  - Dropped files populate the selected-file context + summary.
  - Works for text, PDF, image (route to vision model when enabled).

### Audio picker available everywhere it matters

- **priority:** P1
- **category:** audio / ux
- **description:** The picker should be reachable inline from Clips and
  Services, not only from Settings.
- **acceptance criteria:**
  - A shortcut button on Clips and Services opens the same picker.
  - Current accent theming is preserved.
  - Current categorized list (WASAPI default, dshow devices, loopback
    sounddevice inputs) stays intact.

### World Tracker tab

- **priority:** P1
- **category:** content / news
- **description:** Start the World Tracker concept alongside World
  News: live region-tagged feed with lightweight mini-map.
- **acceptance criteria:**
  - New tab stub; shares news_service fetchers where possible.
  - Placeholder map widget does not block UI on load.
  - Tab is discoverable, configurable, health-checkable, non-blocking,
    safe to fail (per project law).

### 4D Lab live telemetry ring

- **priority:** P1
- **category:** 4d lab / telemetry
- **description:** Extend SRM panel with a rolling N-sample history
  chart (theta/phi/sigma) and a tiny always-on lightweight state feed
  (`emotion`, `thinking`, `speech`) that other tabs can subscribe to.
- **acceptance criteria:**
  - History chart uses the same canvas or adopts a lightweight chart
    library already on disk.
  - Lightweight state feed is a bounded in-memory ring buffer, not a
    network service.
  - Works with the SRM telemetry POST off.

### Crash-audit hook

- **priority:** P1
- **category:** diagnostics
- **description:** Wrap `mainloop()` in a top-level handler that writes
  a dated traceback to `data/crashlogs/` and, on next launch, surfaces
  "last run crashed at X" in the Logs tab.
- **acceptance criteria:**
  - Crash log path per run, with timestamp + Python + OS info.
  - Logs tab displays a one-line banner on boot if a crashlog exists.
  - Log never grows unbounded (keeps last N).

### Dependency / runtime health audit

- **priority:** P1
- **category:** diagnostics
- **description:** Periodic self-check that verifies FFmpeg, Vosk
  model, Ollama, Pillow, mss, sounddevice, and each vision/chat model
  configured in `router` is actually available. Surface results in the
  Services tab.
- **acceptance criteria:**
  - One health-check surface shows pass/fail for each dep.
  - Results update when the user opens the Services tab.
  - No blocking sync I/O on the main thread.

### Generative tools callable from chat

- **priority:** P1
- **category:** chat / generative
- **description:** Image gen, audio gen, video gen should be invokable
  from chat ("make me an image of X") without leaving the Chat tab.
- **acceptance criteria:**
  - Router in chat detects a generative intent and routes to the right
    backend.
  - Result preview is inlined in the chat transcript.
  - Safe fallback when a backend is offline.

---

## P2 — Quality & correctness

### Intent recovery from messy voice transcripts

- **priority:** P2
- **category:** stt / intent
- **description:** Messy STT output still derails Simian into awkward
  replies. Add an intent-recovery pass so noisy / partial transcripts
  still route correctly when intent is obvious.
- **acceptance criteria:**
  - Graceful clarification when phrasing is obviously garbled.
  - Fewer derailed replies caused by partial STT phrases.
  - Confidence-weighted fallback to the most likely canonical intent
    (e.g. "look screen" ↔ "look on my screen").

### Better TTS / STT systems

- **priority:** P2
- **category:** voice
- **description:** Investigate higher-quality local TTS (piper, tts-v2
  variants) and STT (vosk-large, whisper.cpp small) models, with a
  non-breaking opt-in path. Preserve the current chunk rollover.
- **acceptance criteria:**
  - Per-backend setting in Settings tab.
  - Chunk rollover keeps working end-to-end.
  - Latency of first spoken word does not regress.

### Faster screen awareness via lighter vision pre-pass

- **priority:** P2
- **category:** screen awareness
- **description:** Right now a 180 s+ vision miss still produces
  nothing. A lightweight pre-pass (active window + top visible
  windows) can return instantly while the full vision call runs in
  background.
- **acceptance criteria:**
  - Pre-pass returns in <200 ms.
  - Full vision result streams in when available.
  - User-visible answer never waits for the slow path when pre-pass is
    already enough.

---

## P3 — Future intelligence (ML / learning roadmap)

### Supervised learning integration

- **priority:** P3
- **category:** ml / intent
- **description:** Use labeled local interaction data to improve intent
  classification, tool routing, and task prediction.
- **acceptance criteria:**
  - Scaffolded dataset format + versioning.
  - Pluggable intent classifier with clear training entry point.
  - Safe-by-default: training never auto-runs without user opt-in.
  - Use cases covered: voice/STT correction, command intent,
    screen-awareness context classification, replay error-state
    prediction.

### Reinforcement learning / feedback loop

- **priority:** P3
- **category:** ml / personalization
- **description:** Improve Simian over time from user corrections,
  accepted/rejected actions, and successful workflows. Local-only
  reward signals; no autonomous behavior.
- **acceptance criteria:**
  - Reward signal is strictly local and never networked by default.
  - Clear kill switch + "reset personalization."
  - Use cases covered: follow-up quality, intent prioritization, tool
    selection, personalization.

### Data preprocessing tools

- **priority:** P3
- **category:** ml / tooling
- **description:** Scaffold cleaning, transforming, labeling utilities
  for local datasets feeding Simian's ML modules.
- **acceptance criteria:**
  - Versioned dataset layout on disk.
  - Labeling UI skeleton lives alongside the Files tab.
  - CLI and GUI paths both usable.

### Model training libraries + training hooks

- **priority:** P3
- **category:** ml / tooling
- **description:** Practical local training entry points, evaluation
  hooks, dataset loading, metrics capture. Not a tonight task — just
  structure + placeholders.
- **acceptance criteria:**
  - `training/` module tree with placeholder entry points.
  - Metrics capture writes to `data/metrics/` so they can be charted
    later.
  - Works offline.

### Performance metrics + visualization

- **priority:** P3
- **category:** ml / observability
- **description:** Track model accuracy, latency, runtime cost, routing
  decisions, and user feedback. Tie into existing telemetry / 4D Lab.
- **acceptance criteria:**
  - Dashboard tab (or nested in Services) charting the metrics.
  - Charts handle empty data gracefully.
  - No blocking calls on the UI thread.

### Context-aware ML assistance

- **priority:** P3
- **category:** ml / autonomy
- **description:** Simian benefits from ML for: more accurate command
  understanding, improved contextual understanding, automation of
  repetitive tasks, better file management, safe prediction of likely
  next actions.
- **acceptance criteria:**
  - Scoped proposals for each use case land in this backlog as their
    own tasks when picked up.
  - No broad rewrite tonight; structure only.

---

## P4 — Nice-to-have / polish

### Noise reduction in "Best guess desktop audio" log

- **priority:** P4
- **category:** ui / logs
- **description:** Repeated button presses used to spam identical
  `[Audio] Best-guess desktop audio: …` lines. Now only log when the
  guess changes the picker selection. Kept as a backlog item for any
  other log-deduplication passes of the same kind.
- **acceptance criteria:**
  - Dedup similar click-spam in logs across the GUI.
  - No loss of information on genuinely-new events.

### Dark mode / accent polish in toplevels

- **priority:** P4
- **category:** ui / theme
- **description:** Audio picker toplevel inherits accent color via the
  extracted `_apply_accent_to_widget` helper. Extend to any future
  toplevel (crash-audit dialog, file preview, Flourishin sync dialog).
- **acceptance criteria:**
  - All toplevels walk through the same accent helper.
  - No flicker on open.

---

## Appendix — Architecture law we hold ourselves to

Every feature must be: **discoverable** (registered with
`core.feature_registry`), **configurable** (has settings keys),
**health-checkable** (`health()` returns a `HealthState`),
**non-blocking** (long work runs in threads or `after_idle`), and
**safe to fail** (no raise escapes to the Tk main loop).

Every UI change must preserve the current dark Simian UI and accent
system. Every service change must preserve capture-only fallback when
upstream models or devices are unavailable. Every speech change must
preserve the current chunk-rollover TTS behavior.

---

## Appendix — Runtime evidence log (source of truth for priorities)

This section is the raw observed runtime evidence that the P0 list is
built from. Kept inline so we don't regress by forgetting why a given
task made the cut. Append-only.

**2026-04-23**

- `clip_20260423_084641.mp4` — bad replay clip: black screen with
  cursor only. Captured right after a cold app launch.
- `clip_20260423_091641.mp4` — good replay clip from the same session
  after a restart + audio-picker settings change. Proves the replay
  pipeline is intermittently healthy, not fundamentally dead — the
  regression is init-order / desktop-composition / capture-state.
- Screen awareness successfully returned structured context for
  **LibreOffice Writer** (active_app, selected code block, visible
  summary, notable elements). Confirms the vision + capture-only path
  can produce high-quality local context when the model is warm.
- Screen awareness also hit `timeout`, `empty_response`, and two
  distinct HTTP 500 bodies observed in logs:
    - `memory layout cannot be allocated`
    - `model failed to load, this may be due to resource limitations or
      an internal error, check ollama server logs for details`
  Both are VRAM / model-load ceilings on the current hardware; code
  surfaces the lighter-model hint but can't fix the ceiling itself.
- Chat timeout retry was observed once in logs:
  `Local model timed out once; retrying: timed out`. Warmup ping before
  retry landed the second attempt.
- Audio picker reliably shows Stereo Mix + Intel mic. Replay output
  inconsistently contains both streams across cold starts — matches
  the "picker right, init-order wrong" hypothesis.
- TTS sequential chunk rollover continues to work end-to-end on long
  replies. Non-regression rule is active.
- 4D Lab SRM tick previously stuttered tab switches; throttle to
  33ms-active / 250ms-hidden was added to `_srm_tick`.
