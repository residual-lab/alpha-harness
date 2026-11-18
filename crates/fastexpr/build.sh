#!/bin/sh
# Build the browser's Fast Expression parser to WebAssembly, after its tests.
#   build.sh <output.wasm>          write it there
#   build.sh --check <current.wasm> fail if <current.wasm> is not what the crate builds
# Run from the repository root. The crate's rust-toolchain.toml pins Rust and its target, which
# rustup installs on first use; the build is byte for byte the same on any Linux machine.
set -eu
mode=write
if [ "${1:-}" = "--check" ]; then mode=check; shift; fi
target=${1:?output path}
crate=crates/fastexpr
built=$crate/target/wasm32-unknown-unknown/release/fastexpr.wasm

if ! command -v cargo >/dev/null; then
  echo "$crate needs Rust to build: install rustup from https://rustup.rs" >&2
  exit 1
fi
(cd "$crate" && cargo test --quiet && cargo build --quiet --release --target wasm32-unknown-unknown)

if [ "$mode" = check ]; then
  if ! cmp -s "$built" "$target"; then
    echo "$target is stale: $crate changed. Run: sh $crate/build.sh $target" >&2
    exit 1
  fi
else
  cat "$built" >"$target"
fi
