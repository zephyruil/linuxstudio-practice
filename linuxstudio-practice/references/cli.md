# CLI reference

Examples use `run.cmd` from the installed skill. Equivalent: `python scripts/launcher.py`. Full installation uses its own `.venv`. Put `--data-dir <absolute directory>` **before** the subcommand to isolate testing; owner and workers inherit it.

```powershell
.\run.cmd doctor
.\run.cmd login
.\run.cmd login --account example_user
.\run.cmd session status --verify
.\run.cmd session show
.\run.cmd session logout
.\run.cmd catalog
.\run.cmd progress --commands ls mkdir
.\run.cmd job start --commands ls mkdir
.\run.cmd job pending <job_id>
.\run.cmd job status <job_id>
.\run.cmd job submit <job_id> --answers-file C:\Temp\answers.json
.\run.cmd job verify <job_id>
.\run.cmd job cancel <job_id>
```

Only `login` initiates login; `session show` reveals an already-running owner. No command interacts with a browser. `login` reuses a valid session; to change account use the owner's **重新登录 / 切换账号** button, or `session logout` then `login`. The picker offers new account, saved account, optional save, and removal of saved accounts. `--account` on a job is a strict identity guard.

`job start` returns a UUID. Poll pending after a short wait (typically 1–3 seconds; no busy polling). Pending example:

```json
{"status":"awaiting_answers","pending":[{"key":"mkdir:1:1","command":"mkdir","stage":1,"total":3,"task":"actual website prompt"}]}
```

Write only current keys and answer strings:

```json
{"mkdir:1:1":"mkdir example_directory"}
```

Keys include command, stage, attempt. Do not recycle an answer file after keys change. One file can cover all pending commands. `answers_queued` means queued; inspect the next report for server feedback. Maximum answer length is 2000 characters. Never include passwords in answers.

One unfinished job per account is allowed. Cancellation is cooperative: an in-flight POST may finish before it is observed. Already-recorded website progress remains. Jobs wait at most 30 minutes for answers. After unknown POST outcomes, inspect live state before a new job; the client never automatically retries POST.

Reports live in `<data-dir>\jobs\<UUID>`, containing account, prompts, answers, feedback, request timing, wall time, and before/after progress. Wall time includes agent thinking/waiting. Cookies/passwords are excluded. `session status` is local evidence; `--verify` checks the site. `job verify` checks the same account and fresh colors after every stage was confirmed.

Private files: `<data-dir>\private\accounts.dpapi` and `active_session.dpapi`, scoped to the current Windows user. Do not publish or move them to another user. Closing a client leaves the owner logged in. Stopping the owner process is not a website logout; the next authorized login can recover its encrypted session. **退出登录** invalidates the generation, clears retained Cookies and requests server logout.

If HTML/JSON schema changes, fail and inspect the live form; never spoof passing results. Only Linux chapter color `#FE8463` is supported in v1; other chapters require their own adapter and tests.
