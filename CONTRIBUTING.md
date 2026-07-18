# Contributing

Thanks for your interest in GreenlightIQ.

## ⚠️ Contributor License Agreement required

GreenlightIQ is licensed **AGPLv3**, and copyright is currently held solely by the author. That consolidation is deliberate — it preserves the option to offer commercial licenses alongside the open source release.

Accepting a contribution without a CLA would split copyright ownership and permanently remove that option, because relicensing then requires the agreement of every past contributor.

**A CLA is therefore required before any external contribution can be merged.** If you'd like to contribute, please open an issue first so the CLA can be sorted out before you invest time in a pull request.

## Development

- Python 3.11+
- `pip install -e ".[dev]"`
- `ruff check .` and `pytest` before opening a PR

## 🛑 Architecture constraints

This project is coursework with graded structural requirements. Please **do not** submit changes that:

- introduce Docker, Kubernetes, or any container runtime
- introduce serverless functions
- replace a managed cloud service with a self-hosted one

These are prohibited by the assignment brief. See [`AGENTS.md`](AGENTS.md).
