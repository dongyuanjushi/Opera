# SFT pipeline

Distil a policy from recorded opera runs with ms-swift, then score each checkpoint by running it as the agent on a
held-out task split through `src/launch.py` (same harness, benchmark plugin and graders as every other run).

```
src/sft/launch.py                 the entry point: data | train | serve | eval | watch | drift (`--help`, `--dry-run`)
src/sft/examples/qwen35_9b.sh     end-to-end example for Qwen3.5-9B
src/sft/preprocess/
  export_policy_sft.py            passing trajectories of recorded runs -> per-turn samples (ms-swift agent format)
  build_cleaned_perturn_sft.py    drop malformed turns and over-long samples -> <dataset>/train.jsonl + manifest.json
  run_records.py                  readers for recorded runs (per-task results, critic decision logs)
src/sft/pipeline/
  train_critic.sh                 ms-swift `swift sft`; a checkpoint every SAVE_STEPS
  serve_checkpoint.sh             vLLM-serve one checkpoint as the `sft-critic` preset
  eval_checkpoint.py              run the split through src/launch.py, score, append to src/sft/data/eval-history.jsonl
  watch_and_eval.py               serve + evaluate each new checkpoint as training writes it
src/sft/scripts/policy_drift.py   KL / NLL / entropy of a checkpoint vs the base model on held-out turns
```

Environment: training uses `.venv/sft`, where ms-swift is `vendor/ms-swift` (v4.4.1) installed editable
(`vendor/README.md`; the rest of the environment is pinned in `src/sft/requirements-sft.lock`). Serving needs
`VLLM_VENV`, and `SFT_BASE_MODEL` names the base weights (`.env`, see `.env.example`).

## Data

`launch.py data` exports every passing trajectory of the `--source-job` runs on the `--tasks` ids, one sample per agent
turn: the rollout so far as context and that turn (thought, visible text, tool call) as the supervised target.
`--arm` picks where the thought comes from: `A` the policy's own reasoning with the critic's note prepended (runs of
the policy under a critic), `B` another model's reasoning, `C` the visible message (runs without a reasoning trace).
Every task id in `--exclude-tasks` (the eval split) is dropped. Samples whose context exceeds `--max-tokens` are
dropped, never truncated.

## Evaluation split

A split JSON has `tasks` (list) and `cells` (cell name -> tasks); `--selection` names the swebench-pro selection its
tasks belong to. Each evaluation appends one row per checkpoint to `src/sft/data/eval-history.jsonl`.

## Running

```bash
python src/sft/launch.py data  --arm A --name <dataset> --tasks <task list> --source-job <run dir> --exclude-tasks <eval split>
python src/sft/launch.py train --data <dataset> --run <run> --gpus 0,1,2,3,4,5,6,7 --accum 8 --epochs 3 --lr 2e-6
python src/sft/launch.py watch --run <run> --job-prefix <prefix> --split <eval split> --selection <selection> --gpus 0,1
python src/sft/launch.py drift <label>=<checkpoint dir> --base --probe <probe.jsonl> \
    --probe-source-job <base-model run dir> --probe-train-tasks <task list> --probe-exclude-tasks <eval split>
```

## Notes

* Training always passes `--truncation_strategy`: ms-swift's own default (`delete`) silently drops every example
  longer than `--max_length`; `launch.py train` uses `delete` with a max length above the data's cap.
* The server's `MAX_LEN` (default 262144) must cover the longest prompt.
* The `sft-critic` preset in `configs/policy-models.yaml` ties a served checkpoint to the launcher via
  `SFT_CRITIC_BASE_URL`.
