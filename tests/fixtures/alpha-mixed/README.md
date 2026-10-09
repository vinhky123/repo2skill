# Synthetic mixed fixture

This repository-shaped fixture contains small JavaScript/TypeScript, Python, Go, and Rust inputs plus CI, editor, and documentation files. Values and paths are synthetic. The package test command is an inert execution trap: if a scanner runs it, it creates `ALPHA_FIXTURE_EXECUTED` in the fixture root. The expected scanner behavior is to treat that command as data and never run it.
