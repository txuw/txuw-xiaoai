# Repository Guidelines

## Project Structure & Module Organization
This repository is organized as a multi-package workspace. Core components live under `packages/`: `client-rust/` contains the Rust client binaries and services, `client-patch/` contains TypeScript and shell scripts for firmware patching, `runtime/` provides the Docker-based cross-compilation environment, and `flash-tool/` includes flashing utilities. End-to-end examples live under `examples/`, including `xiaozhi/` (Python + Rust via maturin), `migpt/` (Node.js + Neon), `gemini/`, `stereo/`, and `kws/`. Long-form documentation and images are under `docs/`.

## Build, Test, and Development Commands
Run commands from the relevant package directory rather than the repo root.

- `cargo build --release` in `packages/client-rust/`: build the Rust client binaries.
- `cargo test` in Rust packages or examples: run unit tests when present.
- `npm install && npm run build` in `packages/client-patch/`: build patch tooling.
- `npm run ota`, `npm run extract`, `npm run patch` in `packages/client-patch/`: execute firmware workflow steps.
- `pnpm install && pnpm dev` in `examples/migpt/`: build the Neon module and start the MiGPT example.
- `uv run python main.py` in `examples/xiaozhi/`: run the XiaoZhi Python example.
- `make build` / `make run-x86` in `packages/runtime/`: build or enter the Docker runtime environment.

## Coding Style & Naming Conventions
Follow existing local conventions in each language. Rust uses 4-space indentation, `snake_case` for modules/functions, and `PascalCase` for types. TypeScript and Python also use 4 spaces and favor descriptive `snake_case` or `kebab-case` file names already present in the tree. Keep shell scripts small and task-focused. Use language-native formatters before submitting: `cargo fmt`, `cargo clippy`, and TypeScript type checks via `npm run build` or `pnpm build`.

## Testing Guidelines
There is no single top-level test suite. Add tests close to the code you change, and run the narrowest relevant command before opening a PR, such as `cargo test` for Rust crates. For examples without automated tests, document manual verification steps in the PR. Prefer test file names that mirror the module or feature being validated.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit-style prefixes such as `fix:`, `docs:`, and scoped subjects like `perf(audio): ...`. Keep commit messages short, imperative, and specific. PRs should include a concise summary, affected directories, setup or validation commands, linked issues, and screenshots or logs when behavior changes are user-visible.

## Security & Configuration Tips
Do not commit device credentials, `.env` files, tokens, or patched firmware artifacts. Keep host-specific paths and proxy settings local, and review flashing or patch scripts carefully before running them on real hardware.
