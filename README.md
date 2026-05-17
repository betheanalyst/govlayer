# GovLayer

**Constitutional AI-assisted governance protocol on GenLayer.**

GovLayer is a fully on-chain governance system where AI acts as a constitutional reviewer at proposal submission time, humans deliberate and vote, and admins act as stewards. All governance logic lives in a single GenLayer intelligent contract.

---

## Live deployment

- **Network:** GenLayer Studionet (Chain ID: 61999)
- **Contract:** `0xAD5ecc8A49EaaA6D71A9a820b12D359c2D0597fb`
- **RPC:** `https://studio.genlayer.com/api`

---

## What it does

- **AI-audited proposals** — every proposal is evaluated against the DAO constitution by a GenVM LLM at submission time. The AI returns accept / revise / reject with specific constitutional clause citations.
- **Community voting** — proposals that pass AI audit enter a configurable voting window. Any eligible address can vote for or against.
- **Dispute resolution** — rejected or accepted proposals can be disputed by voters or the proposer, triggering a multi-stage AI re-evaluation (up to 3 stages with a 1-hour cooldown between stages).
- **Constitution management** — the governing constitution lives on-chain. Amendments require a `constitution_update` proposal that passes voting and is confirmed by an admin.
- **Multisig admin actions** — sensitive operations (removing an admin, changing eligibility mode) require approval from two different admins within 24 hours.
- **Flexible eligibility** — open (any address), ERC-20 token gated, NFT gated, or whitelist-based participation.

---

## Tech stack

| Layer | Technology |
|---|---|
| Smart contract | Python · GenLayer GenVM |
| Consensus | `gl.vm.run_nondet_unsafe` with structural validator |
| Frontend | Vanilla HTML/CSS/JS · no framework, no build step |
| Fonts | Fraunces (display) · DM Sans (UI) · JetBrains Mono (code) |
| Wallet | MetaMask / injected Web3 wallet |
| Deployment | Vercel (static) |

---

## Repository structure

```
govlayer/
├── public/
│   └── index.html          # Complete frontend — single file, zero dependencies
├── contract/
│   └── govlayer.py      # GenLayer intelligent contract (production version)
├── vercel.json             # Vercel routing config
├── .gitignore
└── README.md
```

---

## Deploy to Vercel

### One-click deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/YOUR_USERNAME/govlayer)

### Manual deploy

1. **Fork or clone** this repository
2. **Connect to Vercel** — go to [vercel.com](https://vercel.com), click "New Project", import this repository
3. **Configure:** Framework preset → **Other**. Root directory → leave blank. Output directory → `public`.
4. **Deploy** — Vercel will detect `vercel.json` and serve `public/index.html` for all routes.

No environment variables required. The contract address and RPC are hardcoded in `public/index.html`.

---

## Run locally

No build step required:

```bash
# Option 1: Python simple server
cd public && python3 -m http.server 3000

# Option 2: Node
npx serve public

# Option 3: Open directly in browser
open public/index.html
```

---

## Using the app

### As a visitor (no wallet)
- Browse all proposals, read the constitution, view governance history
- All data is publicly readable from the contract

### As a participant
1. Install [MetaMask](https://metamask.io) and add GenLayer Studionet:
   - **Network name:** GenLayer Studionet
   - **RPC URL:** `https://studio.genlayer.com/api`
   - **Chain ID:** `61999`
   - **Currency symbol:** `GEN`
2. Click **Connect wallet** in GovLayer
3. Vote on open proposals, raise disputes, submit proposals

### As an admin
- Admin addresses can access the **Admin panel** after connecting
- Sensitive actions (remove admin, change eligibility) go through 2-admin multisig with a 24-hour window

---

## Governance flows

```
STANDARD PROPOSAL
  submit_proposal() → AI audit → pending → vote() × N → finalize_decision() → accepted/rejected

CONSTITUTION UPDATE
  submit_proposal(constitution_update) → AI audit → pending → vote() × N
  → finalize_decision() → pending_constitution_confirm → confirm_constitution_update() → accepted

DISPUTE
  raise_dispute() → under_review → AI re-evaluation → accepted/rejected
  (up to 3 stages, 1-hour cooldown between stages)

MULTISIG ADMIN ACTION
  propose_admin_action(type, params) → pending 24h → approve_admin_action() → executed
```

---

## Contract architecture

The `govlayer.py` contract is a single Python file deployed on GenLayer. Key design decisions:

- All address keys in storage use `.as_hex.lower()` for consistent comparison
- `DynArray` of custom types stored at contract level (`proposal_conflicts`, `proposal_disputes`) rather than inside dataclass fields — avoids GenVM instantiation limitations
- AI audit uses `gl.vm.run_nondet_unsafe` with a structural validator requiring categorical agreement on the `decision` field only
- Timestamps from `gl.message_raw["datetime"]` with a `MIN_VALID_TIMESTAMP` guard against fallback-to-zero corruption

---

## Contract: public API summary

| Method | Type | Description |
|---|---|---|
| `submit_proposal` | write | Submit proposal for AI audit |
| `resubmit_proposal` | write | Revise a needs_revision proposal |
| `cancel_proposal` | write | Cancel a pending/needs_revision proposal |
| `vote` | write | Vote for or against a pending proposal |
| `finalize_decision` | write | Record outcome after voting closes |
| `raise_dispute` | write | Dispute a finalized proposal |
| `confirm_constitution_update` | write | Admin: apply dispute-accepted constitution |
| `add_admin` | write | Admin: add a new admin |
| `remove_admin` | write | Admin: remove admin (3+ admins only) |
| `propose_admin_action` | write | Admin: propose multisig action |
| `approve_admin_action` | write | Admin: approve & execute multisig action |
| `set_governance_params` | write | Admin: update quorum, threshold, durations |
| `set_safety_params` | write | Admin: update dispute stages, cooldown, resubmit cap |
| `set_token_rules` | write | Admin: configure token eligibility |
| `toggle_whitelist` | write | Admin: enable/disable whitelists |
| `manage_whitelist` | write | Admin: add/update whitelist entry |
| `remove_from_whitelist` | write | Admin: remove from whitelist |
| `get_proposal` | view | Get single proposal by ID |
| `list_proposals` | view | Paginated proposal list |
| `get_constitution` | view | Get current constitution text |
| `get_config` | view | Get all governance parameters |
| `get_admins` | view | Get active admin addresses |
| `get_whitelist` | view | Get whitelisted addresses |
| `get_governance_history` | view | Paginated governance event log |
| `get_pending_actions` | view | Get pending multisig actions |

---

## Proposal statuses

| Status | Meaning |
|---|---|
| `pending` | Voting is open |
| `needs_revision` | AI returned revise — proposer must edit and resubmit |
| `accepted` | Proposal passed vote and AI audit |
| `rejected` | Failed vote, quorum, or AI audit |
| `under_review` | Dispute in progress |
| `pending_constitution_confirm` | Constitution update passed vote — awaiting admin confirmation |
| `cancelled` | Cancelled by proposer |

---

## License

MIT
