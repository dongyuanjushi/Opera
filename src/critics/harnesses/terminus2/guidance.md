## Harness capabilities

Use only visible tool observations and supported actions. Tool availability does not establish a task requirement. Commit, patch and artifact requirements come from the task contract, not from the harness name.

Terminus 2 is a command-line agent using JSON command batches in a persistent terminal. Visible source code, command output, artifact contents and service responses can all support diagnosis. A partial screen after a short wait may indicate legitimate execution; silence or timeout alone does not prove a blocked shell. Use an empty keystroke and an appropriate wait when the process is still running.

Control keys are input to the current terminal, not a guarantee that a process exits or the host shell returns. The active application, remote session or guest console may consume them. When recovery is justified, choose the documented control for the observed receiver: `C-c` sends Ctrl-C, `C-d` sends end-of-input, `q` can exit a pager, and the actual heredoc terminator closes a heredoc. Use supported notation, such as "send `C-c` once, no trailing newline", rather than text that may be typed literally.

After an escape, interruption or session change, inspect the resulting prompt before sending commands that require a different shell or environment. Do not put a speculative escape and a host command in the same unconditional batch. A login prompt, guest shell, REPL and host shell are different execution contexts. Do not interrupt legitimate startup or long-running work just because several screen captures are unchanged; refresh the output first if the process may have advanced during review.

Completion has two steps: `task_complete: true`, then the harness asks the agent to confirm. An audited submission finding must reach that confirmation request before the agent answers. This protocol does not itself require a git patch or commit.
