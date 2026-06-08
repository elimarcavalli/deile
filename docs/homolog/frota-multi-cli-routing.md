# Frota Multi-CLI — Roteamento por Stage

Cada etapa do pipeline é executada por um worker CLI distinto.

Stage | Worker | Modelo
--- | --- | ---
classify | aider-worker | openrouter/deepseek/deepseek-v4-flash
refine | goose-worker | openrouter/deepseek/deepseek-v4-pro
implement | codex-worker | gpt-5.1-codex-mini
pr_review | opencode-worker | openrouter/qwen/qwen3-coder
follow_ups | qwen-worker | qwen/qwen3-coder
