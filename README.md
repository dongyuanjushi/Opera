<h1 align="center">Opera: A Verbal Critic Framework for Long-horizon Coding Agents</h1>

<p align="center">
  <a href="https://arxiv.org/abs/2609.33987"><img src="https://img.shields.io/badge/arXiv-2609.33987-B31B1B?logo=arxiv&logoColor=white" alt="arXiv"></a>
</p>

## Installation

```bash
git clone <this repository> opera && cd opera
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e "vendor/xrlenv[swebench-pro,deep-swe]"   # sandboxes + harbor / pier
cp .env.example .env                                                                    # then fill in the placeholders
```

The third-party code opera runs on is vendored and installed editable from `vendor/` (see [vendor/README.md](vendor/README.md)):
[xrlenv](vendor/xrlenv) (container sandboxes, the harbor / pier environment backends) and
[ms-swift](vendor/ms-swift) v4.4.1 (SFT only, in its own `.venv/sft`; see [SFT](#sft)).

## Sandbox runtime

Every task runs in its own container, driven by the benchmark's own framework (harbor for Terminal-Bench 2.1,
SWE-bench Pro and SWE-rebench; pier for DeepSWE). `--environment` picks where those containers live:

- **Local Docker** (`--environment docker`): install [Docker Engine](https://docs.docker.com/engine/install/) with the
  Compose plugin, start the daemon and check `docker info`. No cluster credentials are needed; good for smoke runs
  and small sweeps.
- **xrlenv cluster** (`--environment xrlenv`, the default): [xrlenv](vendor/xrlenv) schedules containers across a
  fleet of CPU nodes and manages the benchmark images; set the three `XRLENV_*` control-plane variables. Its
  documentation is in [vendor/xrlenv/docs](vendor/xrlenv/docs).

## Quick start

```bash
# resolve a run without starting any container
.venv/bin/python src/launch.py tb21 --agent-model gpt-5.6 --selection smoke --dry-run
# three Terminal-Bench 2.1 tasks on local Docker, no critic
.venv/bin/python src/launch.py tb21 --agent-model gpt-5.6 --selection smoke --environment docker --max-workers 1
# the same with the Opera critic
.venv/bin/python src/launch.py tb21 --agent-model gpt-5.6 --critic-model gpt-5.6 --selection smoke --environment docker --max-workers 1
```

## SFT

`src/sft/` distils a policy from its own critic-guided rollouts: the passing trajectories become per-turn samples
(the critic's note is folded into the thought), ms-swift fine-tunes the policy, and every checkpoint is served with
vLLM and run as the agent on a held-out split. Training uses its own environment:

```bash
uv venv .venv/sft --python 3.12
uv pip install --python .venv/sft/bin/python -r src/sft/requirements-sft.lock
uv pip install --python .venv/sft/bin/python --no-deps -e vendor/ms-swift
```

`src/sft/launch.py` is the single entry point (`data | train | serve | eval | watch | drift`, each with `--dry-run`);
`[src/sft/examples/qwen35_9b.sh](src/sft/examples/qwen35_9b.sh)` runs the whole recipe for Qwen3.5-9B:

```bash
bash src/sft/examples/qwen35_9b.sh data     # dataset from recorded critic runs
bash src/sft/examples/qwen35_9b.sh train    # 8 GPUs, effective batch 64, 3 epochs, lr 2e-6
bash src/sft/examples/qwen35_9b.sh watch    # evaluate each checkpoint as it is saved
```

Details: [src/sft/pipeline/README.md](src/sft/pipeline/README.md).

---

## Repository layout

```
src/launch.py          the launcher (benchmark × harness × policy [× critic])
src/critics/           critic core: engine, schedule, applicability, findings, proxy, strategies, harness packs, prompts
src/sft/               SFT: launch.py, examples/, preprocess/, pipeline/, scripts/
plugins/benchmarks/    one harbor / pier job per benchmark: task selections, grading, pass@k, resume
plugins/{terminus2,openhands,mini_sweagent}/   harness subclasses that route the model endpoint through the proxy
configs/               model presets, per-benchmark critic policies, strategies, the experiment protocol
experiments/           launchers for paper experiments
vendor/                third-party code: xrlenv, ms-swift
```

## Cite Us

If you find `Opera` useful in your research, please cite:

```bibtex
@article{mei2026opera,
  title={Opera: A Verbal Critic Framework for Long-horizon Coding Agents},
  author={Mei, Kai and Hu, Zhiyuan and Dai, Yutong and Tan, Juntao and Zhang, Yifan and Song, Dingjie and Metaxas, Dimitris N and Savarese, Silvio and Xu, Ran and Chen, Zeyuan},
  journal={arXiv preprint arXiv:2609.33987},
  year={2026}
}
```
