# experiments/ — launching the paper's experiments

Every launcher goes through `src/launch.py`. Endpoints and keys come from `.env` (copy `.env.example`); the protocol —
harness, task selection, turn cap, reasoning efforts, Opera's critic policy and the ablations — comes from
`configs/experiments.yaml`. Each `.sh` launcher starts its runs detached and logs to `experiments/logs/`; pass
`EXTRA=--dry-run` to print the resolved launch lines instead.

| Launcher | Runs |
|---|---|
| `run.py` | one run: benchmark × policy × critic, optionally an ablation or a baseline strategy |
| `matrix.sh` | the main table on one benchmark: every policy × {no critic, gpt-5.6, claude-opus-4-8, self} |
| `baselines.sh` | the four baseline critics for one policy |
| `ablations.sh` | Opera (full) and its ablations for one policy: edit-only, non-edit, no-audit |
| `sft/*.sh` | policy distillation: data, training, checkpoint evaluation, drift |

## Policy × critic

```bash
python experiments/run.py --bench tb21 --policy qwen38-27b --critic gpt-5.6 --pass-at-k 3          # Opera
python experiments/run.py --bench tb21 --policy qwen38-27b --critic claude-opus-4-8 --pass-at-k 3
python experiments/run.py --bench tb21 --policy qwen38-27b --critic self --pass-at-k 3             # the policy critiques itself
python experiments/run.py --bench tb21 --policy qwen38-27b --critic none --pass-at-k 3             # no-critic control
bash experiments/matrix.sh swebench-pro 3                                                          # all policies × critics
```

* `--bench`: `tb21` (Terminal-Bench 2.1, Terminus 2, 88 tasks, no turn cap), `swebench-pro` (subset-100, OpenHands,
  150 turns), `deepswe` (113 tasks, mini-swe-agent, no turn cap).
* `--policy`: `qwen35-9b`, `muse-glimmer-30b`, `qwen38-27b`, `deepseek-v4-flash`; its reasoning effort per benchmark
  comes from the config (`--agent-effort` overrides).
* `--critic`: `gpt-5.6`, `claude-opus-4-8` (both at `high`), `self`, `none`.
* Opera's critic policy (`critic_policy` in the config) is applied automatically, and critic prompts and requests are
  recorded (`--no-record` turns that off).
* Overrides: `--agent-url http://<host>:<port>/v1` (the policy's endpoint for this run), `--workers`, `--max-turns`,
  `--timeout-sec`, `--selection`, `--harness`, `--job-id`, `--resume`.

## Baseline critics

```bash
bash experiments/baselines.sh swebench-pro qwen38-27b 1          # swe_prm, swe_search, llm_verifier, agentic_rubrics
python experiments/run.py --bench swebench-pro --policy qwen38-27b --critic gpt-5.6 --strategy swe_prm
```

Baselines use the same proxy, review schedule, transcript bound and note delivery as Opera; only the critic's
prompt and how its answer becomes a note differ. `STRATEGIES` and `CRITIC` select a subset / another critic model.

## Ablations

```bash
bash experiments/ablations.sh tb21 qwen38-27b 1                  # full Opera + edit-only + non-edit + no-audit
python experiments/run.py --bench tb21 --policy qwen38-27b --critic gpt-5.6 --ablation no-audit
```

| `--ablation` | Change to Opera |
|---|---|
| `edit-only` | only the edit-stage operators (`requirement_contract_review`, `diff_scope_review`, `state_transition_review`) |
| `non-edit` | every operator except the edit-stage ones |
| `no-audit` | no intervention audit and no close audit |

Ablation runs carry their name in the job id (`…-edit`, `…-noedit`, `…-noaudit`), so they never collide with the
full runs.

## SFT

```bash
EVAL_SPLIT=<eval split json> bash experiments/sft/build_data.sh A <dataset> <task list> <critic run dir>...
GPUS=0,1,2,3,4,5,6,7 ACCUM=8 EPOCHS=3 bash experiments/sft/train.sh <dataset> <run>
EVAL_SPLIT=<eval split json> SELECTION=<selection> GPUS=0,1 bash experiments/sft/eval_watch.sh <run> <job prefix>
EVAL_SPLIT=<eval split json> SELECTION=<selection> bash experiments/sft/eval_once.sh <run> <step> <job prefix>
PROBE=<probe.jsonl> BASE=1 bash experiments/sft/drift.sh <label>=<checkpoint dir> ...
```

These wrap `src/sft/launch.py`; `src/sft/examples/qwen35_9b.sh` runs the whole recipe for Qwen3.5-9B and
`src/sft/pipeline/README.md` describes each stage. `.env` needs `SFT_BASE_MODEL` (the base weights) and `VLLM_VENV`
(a vLLM environment for serving checkpoints).

## Outputs

`results/<bench>/<selection>/<job id>/`: one directory per trial (`result.json`, agent trajectory, verifier output),
`critic-logs/<trial>/decisions.jsonl` (every critic review), `launch.log` and `pass_at_k.json`.
