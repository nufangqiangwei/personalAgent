# runtime-policy

Minimal TypeScript plugin mounted into the official DeepSeek Harness profile.
It provides **only** the permission surface:

- `restrict({allow})` narrows the inherited tool roster;
- `guard(predicate)` rejects every invocation outside the profile allowlist,
  including a tool registered later in the same plugin scope;
- a model transport hook that only permits the configured provider through the
  local gateway, so the single model slot cannot be bypassed.

It contains no business Loop, no orchestration and no state transitions. The
Python engine still validates scope token, epoch, budget and stop flags on every
tool and model call, and `restrict` alone is never treated as a security
boundary.

```powershell
npm ci --prefix code/runtime-policy
npm run build --prefix code/runtime-policy
npm run test --prefix code/runtime-policy -- --run
```

`profile.json` is the declarative restricted profile (empty managed workspace,
no plugin autodiscovery, no shell/fs, no sub-agents, no self-modification, no
implicit SDK retries, no persisted reasoning).
