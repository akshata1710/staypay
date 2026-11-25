# Onboarding: your first two weeks as an SDE-1 on Payments

This repo is set up to feel like joining a real payments team. You get a working service, a
backlog of realistic tickets, incident drills that page you with real symptoms, and runbooks.
Work through it in the order below.

## Day 1: get it running in VS Code

1. Install: Python 3.12, Docker Desktop, VS Code, `git`, `jq`.
2. Open the folder in VS Code. Accept "Install recommended extensions" (Python, Ruff, Docker,
   SQLTools, REST Client, YAML, GitLens...).
3. Pick **one** setup:

   **A. Dev container (zero local setup).** Command Palette → *Dev Containers: Reopen in Container*.
   Postgres, Redis and every Python dependency come with it, and the migrations run for you.

   **B. Local venv.**
   ```bash
   make install        # creates .venv, installs deps, copies .env.example -> .env
   make up             # Postgres + Redis in Docker
   make migrate
   ```
   Then *Python: Select Interpreter* → `.venv`.

4. Run and Debug panel (Ctrl/Cmd+Shift+D) → **"Full stack (API + MockPay + workers)"** → F5.
   Breakpoints work in the API, MockPay and every worker.
5. Open `requests.http` and click *Send Request* on "Checkout: 3 installments". Then open
   http://localhost:8000/docs.
6. Testing panel (beaker icon) → run all. Or `make test` and `make chaos`.
7. SQLTools sidebar → "StayPay local" → browse `charges`, `installments`, `payment_events`.

**Done when:** you made a checkout from `requests.http`, saw the rows in SQLTools, and all tests are green.

## Days 2–3: read the code in this order

| Order | File | Question to answer for yourself |
|---|---|---|
| 1 | `app/db/models.py` | Which tables are the source of truth? Why is money a `BigInteger`? |
| 2 | `app/domain/state_machine.py` | What happens to a late `charge.succeeded` after a refund? |
| 3 | `app/domain/checkout.py` | Two requests with the same key arrive 1ms apart. Walk through both. |
| 4 | `app/domain/charging.py` | The pod is killed right after the processor call. What finishes the charge? |
| 5 | `app/domain/webhooks.py` | Why does the HTTP handler not process the event itself? |
| 6 | `app/domain/refunds.py` + `policies/cancellation.yaml` | Guest paid 1 of 4 installments and cancels late. What happens? |
| 7 | `app/domain/reconciliation.py` | What does finance do with `MISSING_IN_LEDGER`? |
| 8 | `mockpay/app.py` | Which faults can you inject? Which one is the "money moved but we never heard" case? |
| 9 | `tests/chaos/test_chaos.py` | Pick one test, break the code on purpose, and watch it fail. |

Read the ADRs in `docs/adr/` alongside. They explain *why*, which is what reviewers ask about.

## Days 4–5: your first ticket

Start with a `good-first-issue` from [`docs/tickets/BACKLOG.md`](tickets/BACKLOG.md). Work like it's a real team:

```bash
git checkout -b SP-104-idempotency-key-cleanup
# write the failing test first, then the fix
make lint test chaos
git commit -m "SP-104: purge idempotency keys older than 24h"
```
Open a PR and fill in `.github/pull_request_template.md` honestly, especially the payments checklist.
Review your own diff the next morning before asking anyone else to.

## Week 2: on-call shadow

Do the incident drills in [`docs/incidents/`](incidents/). Each one:
1. Run the setup command. That's "the page".
2. **Don't read the answer yet.** Use logs, metrics (`/metrics`, Grafana), SQL ([`docs/SQL_COOKBOOK.md`](SQL_COOKBOOK.md)) and the runbooks.
3. Mitigate first, then find the root cause.
4. Write a short postmortem with [`docs/postmortem-template.md`](postmortem-template.md).

## How SDE-1 work is judged on a payments team

- **Correctness over speed.** A duplicate charge is worse than a slow checkout.
- **Say what you don't know.** "Timeout" means *unknown*, not *failed*. The same goes for status updates to your lead.
- **Small PRs.** One behaviour change per PR, with a test that fails without it.
- **Leave a trail.** Metrics for new failure modes, a runbook line for anything someone may have to do at 3am.
- **Ask early.** After 30–60 minutes stuck, write down what you tried and ask. That's expected, not a weakness.
