## DeepSWE task contract (trusted)

The task is a **feature specification** (not a bug report): the instruction names the modules, functions, behaviours and edge cases to implement. There is no reported failure to reproduce; correctness means the specified behaviour is implemented and exercised by the agent's own focused checks. Hidden tests grade the repository afterwards — never speculate about them.

**Only committed work is graded.** The submission is `git diff <base> HEAD`: the agent must commit the required changes on the requested branch before finishing. Uncommitted required work is a delivery gap for `submission_readiness_review`. A commit does not establish correctness: the same operator must also reconcile specified behavior, coverage and necessary evidence. The agent may keep improving after a commit and commit again; do not direct it to finish while a supported task obligation remains open.

A committed patch that does not **import** scores zero on every test (hidden tests fail at collection). When the agent edits by generated scripts or large rewrites, a visible check that the edited module still loads (`python -c "import <module>"`, `py_compile`, or a focused test run) is a legitimate, high-value `minimal_repro_or_focused_verifier` demand; an edit whose output visibly shows a syntax or structural error is a `failure_signature_triage` finding.

The runtime supplies `<PROGRESS_METADATA>` with `n_commits` and `uncommitted_edits`. Cross-check these transcript-derived heuristics against current raw tool output. A newer observed successful commit or clean status can supersede a stale counter; unsupported agent claims are not evidence.

The container has no git identity. A plain `git commit` fails with "Author identity unknown" and commits NOTHING,
while the agent typically believes it committed. Treat a commit attempt followed by that message (or any `fatal:`)
as no commit; the only working form is `git -c user.name=agent -c user.email=agent@opera.local commit -m "..."`
(or `git config user.name/user.email` first), and a submission direction must spell it out.

Preserve useful task work when repairing a syntax/import error. A whole-file checkout, stash or reset may discard
required implementation; prefer a targeted correction when the visible error supports one. Do not infer that
every revert is wrong: judge its effect on the task obligation from the observed changes. Elapsed time is not
evidence of readiness and must not become a reason to stop or discard work.
