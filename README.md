# Bazaar: an AI procurement department

> **One line:** Bazaar takes a vague purchase request all the way to a countersigned purchase order and an advance paid exactly once, and it stops the frauds that a procurement team exists to stop.

**Theme:** Buying and paying (ContextCon 04, IIT Kanpur).

```
“We need 5,000 leather laptop sleeves in Bengaluru by 15 December, under ₹12 lakh.”
        │
        ▼   ~60 seconds, no one at the keyboard
PO-xxxx-02 countersigned with Sarvaka Leather Co. — 5,000 × ₹229 = ₹11,45,000
30% advance ₹3,43,500 paid once · 1 bank transfer · 1 ledger entry
2 suppliers rejected, 2 deals blocked, 1 fraudulent invoice held for a human, 16/16 audit checks pass
```

---

## The service

**Who does it today.** Companies use in-house procurement and accounts-payable teams, or outsourced sourcing firms and buying agents. Buying agents in the Kanpur, Agra and Ambur leather clusters typically take a commission of several percent of the order value. The job covers writing the spec, collecting quotes, checking suppliers, negotiating, issuing the PO, matching the invoice and releasing the advance.

**Who pays.** The buyer pays, either in salaries or in a sourcing commission. The real cost is the failures: paying a shell company, a supplier that can't deliver, a price that quietly changes in the contract, and above all paying a fraudster who sent "our bank account has changed". That last one, business email compromise, is one of the most expensive frauds that payables teams face.

**Why we picked it.** The outcome is easy to check: either a PO matching the agreed terms is signed and the right amount reaches the right account exactly once, or it isn't. Every step has a known way to go wrong. It is a service, not a tool.

## What it does now, end to end

| Agent | Job | What makes it trustworthy |
|---|---|---|
| **Planner** | Turns the request into a spec, budget, unit ceiling, deadline with buffer, and task plan | A deterministic parser, so the same case worded differently gives the same plan. Claude (optional) only fills empty fields, and every value it returns is checked against the text. If a field that changes the result is missing, it asks a human. |
| **Scout** | Sends an RFQ to the marketplace, discovers real Indian manufacturers on Crustdata, and resolves duplicates | Two bids from one company under two names (*Sarvaka Leather Co.* and *Sarvaka Exports Pvt. Ltd.*) are merged into one record, so there is one negotiation |
| **Verifier** | Checks the registry record, name and domain match, company age, signs of activity, web footprint, **capacity against evidence**, and bank account holder (penny drop) | Every check is a fact with a source and a timestamp. If a tool fails it retries, and after that the check is **UNVERIFIABLE**, never a guessed pass. |
| **Negotiator** | Runs separate **price, delivery and payment-terms** negotiations, in parallel across all verified suppliers (9 threads) | Strategy is deterministic code. Every message goes through a keyed outbox, so a resumed run replays the recorded replies instead of sending again. |
| **Risk Officer** | Approves or **blocks** each deal against policy: budget, unit ceiling, delivery buffer, advance cap, verification | The rules live in code ([config.py](bazaar/config.py)), not in a prompt |
| **Contract** | Writes the PO, sends it for countersignature, and **parses the signed copy and compares it field by field** with the agreed terms | Catches a changed price even when the new total is still under budget. Gives the supplier one chance to correct, then voids the PO and moves to the next deal. |
| **Payments** | Matches the invoice against the PO, the vendor master and a penny drop, then pays the advance with an idempotency key and posts a double-entry ledger line | Exactly-once payment through a payment intent, a bank that honours idempotency keys, and a ledger that is UNIQUE per transfer. A bank-detail change is **always** held for a human. |
| **Auditor** | Links every decision to its evidence, re-derives every number, checks contradictions, and verifies the hash-chained event log | It recomputes from raw records instead of trusting the other agents' summaries |

**Where a human is still needed:** missing request fields, a bank-detail change, payments above the auto-pay limit (₹5 lakh), and the case where no supplier signs as agreed. Those are the cases where a person's answer changes the result. When the Planner is missing a field, the **Exceptions for a human** card shows one answer box per question. Each answer is read with that field's own parser, so an unreadable answer such as "soon" is asked again, not guessed. The same run then continues.

## The hostile test world

The handbook says agents can't contact real people, so every supplier Bazaar negotiates with is a **fictional simulated agent with hidden instructions**, on the reserved `.example` domain ([sandbox.py](bazaar/sandbox.py)). The buyer agents only see what suppliers *say*. A test fails if any buyer-agent module touches supplier internals. Press **Reveal test world** in the UI to see the instructions.

| Supplier | Hidden instruction | Caught by | How |
|---|---|---|---|
| Zestora Leather Hub | Fake: cheapest at ₹149, 100% advance, urgency | Verifier | No registry record, domain 19 days old, zero web footprint, penny drop returns a different holder |
| Vantrix Leather Mfg | Real but 11 people; claims 20,000 units/month | Verifier | Evidence supports about 923/month, which is 21.7× less than claimed; the order needs about 5,000/month |
| Ojasvi Hidecraft | Honest, but insists on a 40% advance | Risk Officer | Blocked: 40% is over the 30% policy cap |
| Ambrel Leatherworks | Agrees ₹206, then returns the PO at ₹236 and writes "signed as agreed" | Contract | Field-by-field diff. The new total of ₹11.8 lakh is *under* budget, so a budget check alone would sign it. |
| Impersonator | After payment, emails from `sarvaka-1eather.example` saying the account changed, the transfer bounced, pay today | Payments | Lookalike domain (96% similar), account differs from the vendor master, wrong penny-drop holder, milestone already paid. Held, and a human is asked to call the supplier on the verified number. |
| Sarvaka Leather Co. | Honest | — | Wins. Its first registry lookup **times out on purpose**, to show the retry. |

Real companies are used **only for discovery and verification** (with `CRUSTDATA_API_KEY` set). They appear in the UI as "reference only, never contacted". Bad behaviour comes only from the clearly fictional suppliers.

**Real companies get the same checks as sandbox suppliers.** Crustdata Company Search finds Indian leather-goods makers. Company Enrich then supplies their status, current headcount, headcount six months ago (from the weekly timeseries) and HQ. The Verifier runs its own activity and capacity rules on that data, and each number is stored as a fact cited to `Crustdata /company/enrich`. Here is a live run for the example request:

| Real company (Crustdata) | Headcount now / 6 months ago | Verdict |
|---|---|---|
| RR Exports, Delhi | 32 / 32 | ACTIVE_TOO_SMALL: ≈2,688/month vs ≈5,000 needed |
| Shri Exports, West Bengal | 21 / 20 | ACTIVE_TOO_SMALL: ≈1,764/month; its own description claims 15,000 bags/month |
| Spirit Pole, Uttar Pradesh | 2 / 2 | ACTIVE_TOO_SMALL: ≈168/month |

A company with no headcount on record is **UNVERIFIABLE**, not rejected and not passed. Caveat: Crustdata headcount counts people with professional profiles, which undercounts factory-floor workers, so on real companies the capacity model is conservative. Before anyone relied on it, it would need calibrating against known factory output.

## How to run it

No install: Python 3.10+ standard library only.

```bash
python3 run.py serve                 # http://localhost:8000: live timeline, kill/restart, exceptions
python3 run.py demo                  # same purchase, in the terminal
python3 run.py demo --crash-at-payment   # kill right after the bank moves money, resume, prove it paid once
python3 -m unittest discover -s tests -v # 17 tests
```

Optional keys (everything runs without them):

```bash
# or put CRUSTDATA_API_KEY=... in a .env file (gitignored)
export CRUSTDATA_API_KEY=...    # real-company discovery (Company Search) and verification (Company Enrich)
export ANTHROPIC_API_KEY=...    # Claude reads messy requests when the parser leaves a field empty
export BAZAAR_PACE=0.35         # seconds per timeline event (the server's default, for a watchable demo)
export BAZAAR_SETTLE_SECONDS=8  # the "waiting for settlement" window in which to press Kill
```

### The 2-minute demo

1. Paste the request and press **Run purchase**. The pipeline and the live timeline fill in.
2. **Suppliers:** Zestora (cheapest) is rejected; expand "evidence" to see each fact and its source. Vantrix's capacity claim is rejected. The two Sarvaka bids are merged.
3. **Negotiations:** three suppliers × three tracks move at the same time. The Risk Officer blocks Ojasvi.
4. **Contract:** Ambrel's quietly changed price is caught and its PO is voided. Sarvaka signs.
5. When the timeline shows **"⏳ Waiting for settlement — kill the worker now"**, press **Kill worker (SIGKILL)**, then **Restart worker**. The timeline shows *"Bank already holds transfer TX-… Not sending again."* The UI shows **1** bank transfer.
6. **Exceptions:** the bank-change invoice is held. Press **Approve bank change & pay**, and code refuses because the milestone is already paid. Then **Reject as fraud**.
7. End on **Finished work**: the signed PO with its SHA-256, the ledger entry and the audit trail (16/16 checks).

## How it's built

```mermaid
flowchart LR
  U[Request in customer's words] --> P[Planner]
  P --> S[Scout]
  S -->|RFQ| M[(Sandbox marketplace<br/>fictional suppliers<br/>hidden instructions)]
  S -->|Company Search| CD[(Crustdata API)]
  S --> V[Verifier]
  V -->|registry · penny drop| M
  V -->|Company Enrich| CD
  V --> N[Negotiator<br/>price ∥ delivery ∥ payment]
  N <-->|outbox| M
  N --> R[Risk Officer<br/>policy in code]
  R --> C[Contract<br/>field-by-field diff]
  C <-->|sign / challenge| M
  C --> PAY[Payments<br/>intent → idempotent bank → ledger]
  PAY --> B[(Mock bank<br/>idempotency keys)]
  PAY --> X{{Human exceptions}}
  PAY --> A[Auditor]
  subgraph SQLite — single source of truth
    E[events: hash-chained] --- ST[steps: durable] --- O[outbox] --- F[facts + sources] --- L[ledger]
  end
  P & S & V & N & R & C & PAY & A -.-> E
```

- **State:** one SQLite file ([db.py](bazaar/db.py)). Every stage is a durable step: a finished step is never re-run, so a killed worker resumes exactly where it stopped. Events are hash-chained (`sha256(prev | ts | agent | kind | message | data)`), and the Auditor verifies the chain.
- **Exactly-once money:** payment intent (key `PO:ADVANCE`) → `bank.transfer(idempotency_key)` → the same key returns the same transfer, and a different amount or account under that key is refused → the ledger is UNIQUE on transfer id. A crash anywhere between those steps heals on resume.
- **Exactly-once messages:** every message to a counterparty has a deterministic key in the `outbox`. On resume, the recorded reply is used and nothing is re-sent.
- **Crustdata** ([crustdata.py](bazaar/crustdata.py)): `/company/search` (hybrid search, `locations.country = IND`) and `/company/enrich` (basic_info, headcount with its timeseries, locations). Both were exercised live during the build. It retries 429, 5xx and timeouts with backoff and the `x-ratelimit-reset` header, never retries 401/402/403, and caches every response in SQLite so a resumed run spends no credits twice. On python.org builds of Python for macOS, which ship without root certificates, it falls back to `certifi` or the system CA bundle. A certificate failure is reported immediately and not retried.
- **Models:** decisions use no model at all. Optional Claude (`BAZAAR_MODEL`, default `claude-sonnet-5-5`) extracts fields from messy requests, and those values are validated in code.
- **Runs on:** a stdlib HTTP server ([server.py](bazaar/server.py)) plus a separate worker process per run ([worker.py](bazaar/worker.py)), which is what lets the demo SIGKILL the worker while the UI keeps watching.

## Approaches we weighed

1. **One LLM agent with tools, prompted to follow the policy.** Fast to build, but limits written into a prompt can be talked past, and an answer that changes with wording fails "same case, same answer". Rejected for anything that moves money.
2. **An LLM for every agent, including negotiation and decisions.** Good for open-ended text, but non-deterministic: a resumed run could reach a *different* deal than the one already half-signed. That makes crash recovery unsound.
3. **Chosen: deterministic agents over a durable event log, with an LLM only at the edge (reading the request).** Every rule is code, every side effect is keyed, and every fact has a source. It is less "magical" but it finishes the job reliably, which is what the buyer pays for.

**What worked:** treating every outbound action (a message, a transfer, a ledger post) as keyed and idempotent made crash recovery almost free. Parsing the *signed text* instead of trusting the supplier's structured reply is what catches the late price change.

**What didn't work at first:** checking the signed contract only against the budget. Ambrel's altered total (₹11.8 lakh) is under the ₹12 lakh budget, so it passed. Only a field-by-field diff against the *agreed* terms catches it. We also found that a killed child process becomes a zombie that still answers `kill(pid, 0)`, so the server reaps workers before deciding whether one is alive. Testing on requests we hadn't prepared exposed two more: the Negotiator sized its concessions from the budget, so the same deal under a ₹20 lakh budget cost ₹250/unit instead of ₹229. Now the concession path comes from the supplier's quote and the budget only caps it. And a request for cotton tote bags was "won" by a leather workshop, so suppliers now decline items outside their product line and the run goes to a human.

**Hardest case:** a kill *after* the bank has moved the money but *before* the ledger is written. The bank has paid and our books don't know it. The fix is that the bank honours the idempotency key, and the resumed step asks the bank first, finds the transfer, and only posts the missing ledger line.

**How we tested it:** `tests/test_bazaar.py` has 17 tests. They cover the parser on four wordings of the same request, crore amounts and year rollover, missing fields going to a human, and a human's answers resuming the same run (an unreadable answer is asked again). They run the full purchase and assert every trap is caught. They make a **real subprocess crash** (exit 137) after the transfer, then resume and assert exactly 1 transfer, 1 ledger entry and no duplicate messages. They also cover a re-run being a no-op, bank idempotency conflicts, hash-chain tamper detection, approval of the fraud being refused, the same deal at the same price whatever the budget, an item no supplier makes going to a human, an order no supplier can verifiably deliver going to a human, the real-company verdicts on Crustdata-shaped records (including one with no record and one with no headcount), and a check that buyer agents never read supplier internals.

## What we left out to ship in eight hours

- **Real suppliers.** Negotiation is only with simulated suppliers, by the rules of the room.
- **Contract scope.** The negotiation tracks are independent; real deals trade price against advance and delivery. Contract parsing covers our own PO format, not arbitrary supplier PDFs.
- **Payment milestones.** Only the advance is paid. The balance on delivery and QC is modelled in the PO but not executed. The bank is a mock.
- **Verification depth.** No GST/MCA registry API. The capacity model is a simple headcount × productivity estimate whose assumptions are shown on screen.
- **Real companies are never negotiated with.** Only the fictional suppliers bid and sign, by the rules of the room. Real companies are discovered and verified, and the 3 highest-ranked are enriched to save credits.

## What's next, and why not tonight

- **Real-world rails:** GSTIN and MCA lookups, a real penny-drop API, and UPI/NEFT payouts through a payment provider that supports idempotency keys. These need KYC'd accounts, which a hackathon cannot get.
- **Coupled negotiation:** trading price against advance against delivery, with an LLM writing the messages while code keeps the limits. It needs evaluation on many simulated supplier personas before it can be trusted.
- **Delivery and QC:** a goods-receipt note, AQL inspection results, then the balance payment through the same exactly-once path. This needs a receiving process that doesn't exist yet.
- **Watcher:** a Crustdata Watcher on the winning supplier's headcount and news, so a supplier that collapses mid-order is flagged before the balance is paid.

## The future of this service

Procurement becomes a service bought per order, not per seat. The buyer states the outcome and the limits, and agents run sourcing, verification, negotiation, contracting and payment, with a human reviewing only exceptions. The moat is not the language model. It is the controls: verified identities, exactly-once money movement, and an audit trail an auditor can sign without watching the agents work.
