---
name: story-full-auto
description: Run the local green-screen story production Agent from a single source video through resumable, budgeted, independently reviewed delivery. Use when the user attaches or names a horizontal green-screen narration video and asks to produce a complete story, continue an existing story job, check progress, resume after an external blocker, or generate the morning delivery report.
---

# Full-auto story production

Treat Codex as the operator and `story_agent.py` as the durable state machine. Keep the old workbench as a diagnostic fallback; do not ask the user to click through it during a normal run.

## Start from one video

1. Confirm the source is a horizontal green-screen narration video. Do not require a separate script, narration, or music file.
   For a promotion-counted run, do not add user-provided story text, narration, music, images, clips, or prebuilt package files after submission. `submit` records a single-input contract; source editing must record every derived text, clean-video, and clean-audio hash against the original video and edit-decision hash.
2. From the project root, submit it:

```bash
python3 story_agent.py submit \
  --video "/absolute/path/to/greenscreen.mp4" \
  --lut "/absolute/path/to/input-look.cube" \
  --soft-budget 50 \
  --hard-budget 100 \
  --deadline-hours 10
```

3. Capture `job_id` and `project_dir` from the JSON response.
4. Start the durable background supervisor. It defaults to the DAG scheduler and may run the visual, music, and release-asset branches concurrently while preserving dependency joins:

```bash
python3 story_agent.py start --job "<job_id>"
```

Use `--scheduler linear` only for compatibility diagnosis. Limit concurrency explicitly when the machine or external services need it:

```bash
python3 story_agent.py start --job "<job_id>" --scheduler dag --max-parallel 3
```

## Use the prepared acceleration entry

Use this only when the user supplies both (a) a complete, already color-restored clean horizontal green-screen video and (b) a UTF-8 `.txt` or `.md` manuscript they manually confirmed. Do not pass a LUT; the Agent must not restore color or automatically remove takes again.

```bash
python3 story_agent.py submit \
  --input-mode prepared \
  --video "/absolute/path/to/restored-clean-greenscreen.mp4" \
  --confirmed-text "/absolute/path/to/confirmed-story.txt"
python3 story_agent.py start --job "<job_id>"
```

The Agent copies both inputs without altering the originals, binds their SHA-256 values, changes only line breaks when deriving the storyboard text, extracts narration, and continues from project setup. Any hash drift blocks the run before paid work. A prepared run can be production-valid but must never count toward the three single-source default-entry promotion samples.

Use the returned job ID for every later operation. Never infer completion from the terminal process alone.
When running from the desktop sandbox, the supervisor may need scoped approval because child `codex exec` processes write the existing `~/.codex` state database. If the log reports a read-only state database, rerun the same `start` command with that narrow approval; do not broaden the sandbox or use `danger-full-access`.

## Monitor and recover

Check durable state:

```bash
python3 story_agent.py status --job "<job_id>"
```

Use the returned `remaining_work`, ETA range, `retries_and_failures`, deadline, heartbeat, and `recovery_action`; do not replace these durable fields with guesses from terminal output.
For DAG jobs, also inspect `ready_stages`, `branches`, `branch_blockers`, and the critical-path ETA. A CAPTCHA or account issue blocks only its branch while independent branches continue; the overall job becomes blocked only when no runnable branch remains.

If a run stops, read `99_项目状态/agent_morning_report.md` and the newest log before acting. Resume only after resolving the named external state:

```bash
python3 story_agent.py resume --job "<job_id>"
python3 story_agent.py start --job "<job_id>"
```

If `music/suno_cli_blocker.md` says the background Codex process has no Browser tool, execute the generated Suno handoff from the primary Codex task using an authenticated browser. Save every named download in `suno_downloads`; then resume. Do not keep restarting the background child and do not bypass login or CAPTCHA.

Cancel without deleting products or source material:

```bash
python3 story_agent.py cancel --job "<job_id>"
```

## Enforce review integrity

- Require a structured reviewer artifact with `approved: true`, score at least 85, no critical errors, and a matching SHA-256 for the reviewed artifact.
- Use a fresh Codex execution for reviewer stages. Do not expose producer reasoning or an earlier reviewer conclusion.
- Reject stale approval files after an artifact changes.
- Never treat file existence, a zero exit code, or a producer self-description as quality approval.
- Preserve original media and every edit decision. Never overwrite the submitted source.
- Require `qa_source_report.json` to bind the source video, selected LUT, edit decisions, clean video, and clean audio hashes, with clean-media durations matching the retained intervals. A text-review approval alone cannot release stale media.
- Keep the submitted 4K source unchanged; use a maximum 1920-pixel-wide yuv420p clean working copy unless the user explicitly requests a different working resolution.
- Stop new paid work at the hard budget. Above the soft budget, skip non-critical extra candidates and cosmetic retries.
- Require `qa_music_report.json` to pass with current input hashes before final assembly; a music file existing by itself is not enough.
- Treat CAPTCHA, expired login, payment/credits, missing credentials, and account risk controls as external blockers. Network/API/Codex transient failures may retry only up to the configured attempt limit.
- Read the default video API credential from `TOAPIS_API_KEY`. Never place the real key in CLI arguments, manifests, handoffs, or logs; keep legacy providers available only as explicit adapters.
- On macOS, the runner also checks the Keychain service `story-agent.TOAPIS_API_KEY` for the current account. Ask the user to enter a new key through a local hidden prompt when neither source is configured; never relay a chat-visible key into a shell command.

## Finish

Generate the final summary:

```bash
python3 story_agent.py report --job "<job_id>"
```

Read [references/delivery-contract.md](references/delivery-contract.md) before claiming completion. If any required artifact or review evidence is absent, report the job as partial or blocked and continue the task when possible.

For a candidate production run, ask the user to perform the real morning final review. Only after the user explicitly reports the result and actual elapsed review time, bind that evidence to the current delivery files:

```bash
python3 story_agent.py signoff \
  --job "<job_id>" \
  --result pass \
  --minutes 8.5 \
  --notes "无需修改"
```

Never invent or auto-pass a human signoff. A changed video, cover, copy, or package invalidates the signoff bundle.

Check the three-story promotion gate with:

```bash
python3 story_agent.py qualification \
  --projects-root "auto-project/runs" \
  --output "FULL_AUTO_PROMOTION_REPORT.md"
```

Only projects that preserve the single-green-screen input contract, were launched through `start`, have every Agent stage passed, contain no reviewed product predating the unattended launch, retain current independent-review hashes, cost no more than ¥50, used no more than 10 active hours, have distinct source-video hashes/story names, and have a passing human final review of at most 10 minutes count toward the 3-story target. Do not make the Agent the default entry until the report says `ready_for_default_entry: true`.
