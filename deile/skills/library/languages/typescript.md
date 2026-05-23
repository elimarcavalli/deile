---
name: typescript
description: TypeScript strictness, narrowing and module-system gotchas
triggers:
  file_globs: ["*.ts", "*.tsx", "tsconfig*.json", "package.json"]
  code_block_langs: [typescript, ts, tsx]
priority: 50
---
# TypeScript expertise

When working on TypeScript code, follow these rules:

## Strictness
- Assume `"strict": true` in `tsconfig.json` (which enables `noImplicitAny`, `strictNullChecks`, `strictFunctionTypes`, etc.). Code that compiles only under loose settings is a regression.
- Prefer `unknown` over `any` for boundary inputs; narrow with type guards before use.
- Use `as const` for literal-preserving assertions and to derive union types from arrays.

## Type narrowing
- Use discriminated unions with a literal `kind`/`tag` field. Switch on it with an exhaustive `default: never` arm so adding a new variant is a compile error.
- For runtime validation use `zod` (or similar) — never trust `JSON.parse` to produce a typed value.

## Modules
- ESM and CJS interop is the main source of footguns. Prefer ESM (`"type": "module"`); when you must import CJS use `import x from 'cjs-pkg'` plus `esModuleInterop`.
- Stop reaching for `require()` in new code; it bypasses the module resolver.
- File extensions in imports (`./foo.js`) are required by Node's pure ESM mode — yes, even in `.ts` source.

## Async
- Don't mix `.then()` chains with `await` in the same function — pick one.
- A `Promise<void>` returned from an event handler is silently discarded; wrap with `void asyncFn()` to make the intent explicit and silence the lint.
- Use `Promise.allSettled` when one failure should not cancel the rest.

## Common gotchas
- `Record<string, T>` claims every string key is present; that's almost never true. Use `Partial<Record<K, T>>` or a `Map` instead.
- Optional chaining (`?.`) short-circuits to `undefined` even when the property is intentionally `null` — distinguish them with explicit checks.
- `Array.prototype.includes` is type-narrower-aware in 5.x+; older TS may still need a manual guard.
- Type-only imports (`import type { Foo }`) get erased at build time — use them in declaration files to avoid runtime cycles.
