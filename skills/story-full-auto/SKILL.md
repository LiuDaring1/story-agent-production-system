---
name: story-full-auto
description: Run the local green-screen story production Agent from a single source video through resumable, budgeted, independently reviewed delivery. Use when the user attaches or names a horizontal green-screen narration video and asks to produce a complete story, continue an existing story job, check progress, resume after an external blocker, or generate the morning delivery report.
---

# Full-auto story production

Treat Codex as the operator and `story_agent.py` as the durable state machine. Keep the old workbench as a diagnostic fallback; do not ask the user to click through it during a normal run.

## Start from one video

1. Confirm the source is a horizontal green-screen narration video. Do not require a separate script, narration, or music file.
2. From the project root, submit it:

```bash
python3 story_agent.py submit \
  --video "/absolute/path/to/greenscreen.mp4" \
  --soft-budget 50 \
  --hard-budget 100 \
  --deadline-hours 10
```

3. Capture `job_id` and `project_dir` from the JSON response.
4. Start the durable background supervisor with isolated Codex-native stages:

```bash
python3 story_agent.py start --job "<job_id>"
```

Use the returned job ID for every later operation. Never infer completion from the terminal process alone.
When running from the desktop sandbox, the supervisor may need scoped approval because child `codex exec` processes write the existing `~/.codex` state database. If the log reports a read-only state database, rerun the same `start` command with that narrow approval; do not broaden the sandbox or use `danger-full-access`.

## Monitor and recover

Check durable state:

```bash
python3 story_agent.py status --job "<job_id>"
```

If a run stops, read `99_项目状态/agent_morning_report.md` and the newest log before acting. Resume only after resolving the named external state:

```bash
python3 story_agent.py resume --job "<job_id>"
python3 story_agent.py start --job "<job_id>"
```

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
- Stop new paid work at the hard budget. Above the soft budget, skip non-critical extra candidates and cosmetic retries.
- Require `qa_music_report.json` to pass with current input hashes before final assembly; a music file existing by itself is not enough.
- Treat CAPTCHA, expired login, payment/credits, missing credentials, and account risk controls as external blockers. Network/API/Codex transient failures may retry only up to the configured attempt limit.

## Finish

Generate the final summary:

```bash
python3 story_agent.py report --job "<job_id>"
```

Read [references/delivery-contract.md](references/delivery-contract.md) before claiming completion. If any required artifact or review evidence is absent, report the job as partial or blocked and continue the task when possible.
