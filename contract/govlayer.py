# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import typing
from dataclasses import dataclass
from datetime import datetime, timezone
from genlayer import *


# ---------------------------------------------------------------------------
# Storage Dataclasses
# ---------------------------------------------------------------------------

@allow_storage
@dataclass
class ConstitutionalConflict:
    clause: str
    violation: str


@allow_storage
@dataclass
class DisputeRecord:
    stage: u32
    reason: str
    resolution: str
    timestamp: u64


@allow_storage
@dataclass
class GovernanceChange:
    change_type: str
    actor: str          # stored as hex string — Address comparison is unsafe in TreeMap
    details: str
    timestamp: u64


@allow_storage
@dataclass
class Proposal:
    id: u256
    proposer: str       # stored as hex string for safe comparison
    title: str
    description: str
    proposal_type: str
    proposed_constitution: str
    constitution_snapshot: str
    status: str
    ai_audit_decision: str
    ai_audit_reasoning: str
    submitted_at: u64
    voting_start: u64
    voting_end: u64
    votes_yes: u256
    votes_no: u256
    dispute_count: u256
    last_dispute_at: u64
    resubmit_count: u256


@allow_storage
@dataclass
class PendingAction:
    action_id: str
    action_type: str    # "remove_admin" | "set_eligibility_mode" | "constitution_update_propose"
    params: str         # JSON-encoded action parameters
    proposer: str       # hex address of first approver
    proposed_at: u64
    status: str         # "pending" | "executed" | "expired"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZERO_ADDRESS: str          = "0x0000000000000000000000000000000000000000"
DEFAULT_MIN_VOTING_DURATION: int = 3600       # 1 hour in seconds
DEFAULT_MAX_VOTING_DURATION: int = 2592000    # 30 days in seconds
MIN_VALID_TIMESTAMP: int   = 1_700_000_000    # Nov 2023 — any real tx is above this
MULTISIG_WINDOW_SECONDS: int = 86400          # 24 hours for second admin approval


# ---------------------------------------------------------------------------
# Timestamp Helper
# ---------------------------------------------------------------------------

def _get_now() -> u64:
    try:
        raw: str = gl.message_raw["datetime"]
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        if ts < MIN_VALID_TIMESTAMP:
            raise gl.vm.UserError(
                f"Timestamp {ts} is below minimum valid value {MIN_VALID_TIMESTAMP}. "
                "The runtime datetime field appears malformed."
            )
        return u64(ts)
    except gl.vm.UserError:
        raise
    except Exception:
        raise gl.vm.UserError(
            "Unable to read transaction timestamp from runtime. "
            "gl.message_raw['datetime'] is missing or unparseable."
        )


# ---------------------------------------------------------------------------
# AI Output Normalizer
# ---------------------------------------------------------------------------

def normalize_ai_output(data_input: typing.Union[dict, str], context: str = "audit") -> dict:
    if isinstance(data_input, str):
        try:
            data = json.loads(data_input)
        except json.JSONDecodeError:
            if context == "dispute":
                return {"decision": "reject", "reasoning": "AI returned malformed output; defaulting to reject."}
            return {"decision": "reject", "reasoning": "AI returned malformed output; defaulting to reject.", "constitutional_conflicts": []}
    elif isinstance(data_input, dict):
        data = data_input
    else:
        if context == "dispute":
            return {"decision": "reject", "reasoning": "Unexpected AI output type."}
        return {"decision": "reject", "reasoning": "Unexpected AI output type.", "constitutional_conflicts": []}

    if "confidence" in data:
        try:
            data["confidence"] = round(float(data["confidence"]), 2)
        except (TypeError, ValueError):
            pass

    if "constitutional_conflicts" in data and isinstance(data["constitutional_conflicts"], list):
        for conflict in data["constitutional_conflicts"]:
            if isinstance(conflict, dict):
                if "clause" in conflict:
                    conflict["clause"] = str(conflict["clause"]).strip()
                if "violation" in conflict:
                    conflict["violation"] = str(conflict["violation"]).strip()

    if "decision" in data:
        data["decision"] = str(data["decision"]).lower().strip()
    if "reasoning" in data:
        data["reasoning"] = str(data["reasoning"]).strip()

    return data


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

class GovLayer(gl.Contract):

    constitution: str
    proposals: TreeMap[str, Proposal]
    proposal_count: u256
    votes: TreeMap[str, TreeMap[str, bool]]   # votes[proposal_id][voter_hex] = bool

    # Proposal sub-collections — stored at contract level to avoid
    # inline DynArray instantiation of custom @allow_storage types,
    # which the GenLayer runtime does not support.
    proposal_conflicts: TreeMap[str, DynArray[ConstitutionalConflict]]
    proposal_disputes:  TreeMap[str, DynArray[DisputeRecord]]

    # Admin system — keyed by hex string to avoid Address TreeMap comparison bug
    admins: TreeMap[str, bool]
    admin_list: DynArray[str]                 # stores .as_hex strings
    max_admins: u256

    # Governance history
    governance_history: DynArray[GovernanceChange]
    governance_history_count: u256            # maintained count for O(1) pagination

    # Eligibility
    voting_token: Address
    min_tokens_to_vote: u256
    min_tokens_to_propose: u256
    allowed_voters: TreeMap[str, bool]        # keyed by .as_hex
    allowed_proposers: TreeMap[str, bool]     # keyed by .as_hex
    use_whitelist_for_voting: bool
    use_whitelist_for_proposing: bool

    # Governance parameters
    min_quorum: u256
    approval_threshold_percent: u256
    min_voting_duration: u64
    max_voting_duration: u64

    # Eligibility mode
    eligibility_mode: str                     # "open" | "erc20" | "nft" | "custom"

    # Admin-configurable safety parameters
    max_dispute_stages_count: u256            # max dispute stages per proposal
    dispute_cooldown_secs: u256               # seconds between dispute stages
    max_resubmissions: u256                   # max resubmit attempts per proposal

    # Multisig pending actions
    pending_actions: TreeMap[str, PendingAction]
    pending_action_count: u256


    # -----------------------------------------------------------------------
    # Initialisation
    # -----------------------------------------------------------------------

    def __init__(self, initial_constitution: str):
        self.constitution   = initial_constitution
        self.proposal_count = 0
        self.min_quorum     = 3
        self.approval_threshold_percent = 60
        self.min_voting_duration = u64(DEFAULT_MIN_VOTING_DURATION)
        self.max_voting_duration = u64(DEFAULT_MAX_VOTING_DURATION)

        deployer     = gl.message.sender_address
        deployer_hex = deployer.as_hex.lower()
        self.admins[deployer_hex] = True
        self.admin_list.append(deployer_hex)
        self.max_admins = 5

        self.eligibility_mode            = "open"
        self.min_tokens_to_vote          = 0
        self.min_tokens_to_propose       = 0
        self.use_whitelist_for_voting    = False
        self.use_whitelist_for_proposing = False
        self.voting_token = Address(ZERO_ADDRESS)

        self.max_dispute_stages_count = 3
        self.dispute_cooldown_secs    = 3600
        self.max_resubmissions        = 3

        self.pending_action_count     = 0
        self.governance_history_count = 0

        self._log_governance_change("contract_initialized", deployer_hex, "GovLayer deployed")


    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _log_governance_change(self, change_type: str, actor: str, details: str):
        self.governance_history.append(GovernanceChange(
            change_type=change_type,
            actor=actor,
            details=details,
            timestamp=_get_now(),
        ))
        self.governance_history_count += 1

    def _active_admin_count(self) -> int:
        count = 0
        for a_hex in self.admin_list:
            if self.admins.get(a_hex, False):
                count += 1
        return count

    def _is_admin(self, addr: Address) -> bool:
        return self.admins.get(addr.as_hex.lower(), False)

    def _get_token_balance(self, token: Address, user: Address) -> u256:
        if token == Address(ZERO_ADDRESS):
            return 0
        try:
            if self.eligibility_mode in ("nft", "erc20"):
                return u256(gl.call(token, "balanceOf", [user]))
            elif self.eligibility_mode == "custom":
                return 0
            else:
                return 1
        except Exception:
            return 0

    def _check_voting_eligibility(self, user: Address):
        if self.use_whitelist_for_voting:
            if not self.allowed_voters.get(user.as_hex.lower(), False):
                raise gl.vm.UserError("Not whitelisted to vote")
        if self.eligibility_mode != "open":
            balance = self._get_token_balance(self.voting_token, user)
            if self.eligibility_mode == "nft":
                if balance == 0:
                    raise gl.vm.UserError("Must hold the governance NFT to vote")
            elif self.eligibility_mode == "erc20":
                if balance < self.min_tokens_to_vote:
                    raise gl.vm.UserError(f"Insufficient tokens to vote. Required: {self.min_tokens_to_vote}")

    def _check_proposal_eligibility(self, user: Address):
        if self.use_whitelist_for_proposing:
            if not self.allowed_proposers.get(user.as_hex.lower(), False):
                raise gl.vm.UserError("Not whitelisted to propose")
        if self.eligibility_mode != "open":
            balance = self._get_token_balance(self.voting_token, user)
            if self.eligibility_mode == "nft":
                if balance == 0:
                    raise gl.vm.UserError("Must hold the governance NFT to propose")
            elif self.eligibility_mode == "erc20":
                if balance < self.min_tokens_to_propose:
                    raise gl.vm.UserError(f"Insufficient tokens to propose. Required: {self.min_tokens_to_propose}")


    # -----------------------------------------------------------------------
    # Admin management
    # -----------------------------------------------------------------------

    @gl.public.write
    def add_admin(self, new_admin: str):
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can add admins")

        new_admin_hex = new_admin.lower()
        if new_admin_hex == ZERO_ADDRESS:
            raise gl.vm.UserError("Cannot add the zero address as admin")
        if self.admins.get(new_admin_hex, False):
            raise gl.vm.UserError("Address is already an active admin")
        if self._active_admin_count() >= int(self.max_admins):
            raise gl.vm.UserError(
                f"Admin limit reached ({self.max_admins}). "
                "Remove an existing admin before adding a new one."
            )

        self.admins[new_admin_hex] = True

        already_listed = False
        for a_hex in self.admin_list:
            if a_hex == new_admin_hex:
                already_listed = True
                break
        if not already_listed:
            self.admin_list.append(new_admin_hex)

        self._log_governance_change("admin_added", sender.as_hex.lower(), f"Added {new_admin_hex}")

    @gl.public.write
    def propose_admin_action(self, action_type: str, params: str) -> str:
        """
        First step of 2-admin multisig for sensitive operations.
        Supported action_types: 'remove_admin', 'set_eligibility_mode',
        'constitution_update_propose'.
        params: JSON string encoding action parameters.
        Returns the action_id for the second admin to reference.
        """
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can propose admin actions")

        valid_types = {"remove_admin", "set_eligibility_mode", "constitution_update_propose"}
        if action_type not in valid_types:
            raise gl.vm.UserError(
                f"Invalid action_type. Choose: {', '.join(sorted(valid_types))}"
            )

        # Validate params is parseable JSON
        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError:
            raise gl.vm.UserError("params must be valid JSON")

        # Validate params for each action type
        if action_type == "remove_admin":
            if "target" not in params_dict:
                raise gl.vm.UserError("remove_admin requires params: {\"target\": \"0x...\"}")
            target_hex = params_dict["target"].lower()
            if not self.admins.get(target_hex, False):
                raise gl.vm.UserError("Target is not an active admin")
            if self._active_admin_count() <= 2:
                raise gl.vm.UserError(
                    "Cannot propose admin removal: at least 2 admins must remain after removal. "
                    "With only 2 admins, removing one would leave a single admin."
                )

        elif action_type == "set_eligibility_mode":
            if "mode" not in params_dict:
                raise gl.vm.UserError("set_eligibility_mode requires params: {\"mode\": \"open|erc20|nft|custom\"}")
            if params_dict["mode"] not in ["open", "erc20", "nft", "custom"]:
                raise gl.vm.UserError("mode must be: open, erc20, nft, or custom")
            if params_dict["mode"] == "custom" and (
                self.min_tokens_to_vote > 0 or self.min_tokens_to_propose > 0
            ):
                raise gl.vm.UserError(
                    "Cannot propose 'custom' mode while token thresholds are non-zero."
                )

        elif action_type == "constitution_update_propose":
            if "proposal_id" not in params_dict:
                raise gl.vm.UserError(
                    "constitution_update_propose requires params: {\"proposal_id\": \"1\"}"
                )

        self.pending_action_count += 1
        action_id = str(self.pending_action_count)
        now = _get_now()

        self.pending_actions[action_id] = PendingAction(
            action_id=action_id,
            action_type=action_type,
            params=params,
            proposer=sender.as_hex.lower(),
            proposed_at=now,
            status="pending",
        )

        self._log_governance_change(
            "admin_action_proposed", sender.as_hex.lower(),
            f"Action #{action_id} type:{action_type} params:{params}"
        )
        return action_id

    @gl.public.write
    def approve_admin_action(self, action_id: str):
        """
        Second step of 2-admin multisig. A different admin approves and executes
        the pending action. Must be called within 24h of the proposal.
        """
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can approve admin actions")

        if action_id not in self.pending_actions:
            raise gl.vm.UserError("Pending action does not exist")

        action = self.pending_actions[action_id]

        if action.status != "pending":
            raise gl.vm.UserError(f"Action is no longer pending (status: {action.status})")

        if action.proposer == sender.as_hex.lower():
            raise gl.vm.UserError(
                "Cannot approve your own proposed action. A different admin must approve."
            )

        now = _get_now()
        if int(now) - int(action.proposed_at) > MULTISIG_WINDOW_SECONDS:
            action.status = "expired"
            raise gl.vm.UserError(
                f"Action #{action_id} has expired (24h window elapsed). "
                "Propose the action again."
            )

        action.status = "executed"
        params_dict = json.loads(action.params)

        if action.action_type == "remove_admin":
            target_hex = params_dict["target"].lower()
            if not self.admins.get(target_hex, False):
                raise gl.vm.UserError("Target is no longer an active admin")
            if self._active_admin_count() <= 1:
                raise gl.vm.UserError("Cannot remove the last admin")
            self.admins[target_hex] = False
            self._log_governance_change(
                "admin_removed", sender.as_hex.lower(),
                f"Removed {target_hex} via multisig action #{action_id}"
            )

        elif action.action_type == "set_eligibility_mode":
            mode = params_dict["mode"]
            if mode not in ["open", "erc20", "nft", "custom"]:
                raise gl.vm.UserError("Invalid mode in stored action params")
            self.eligibility_mode = mode
            self._log_governance_change(
                "eligibility_mode_updated", sender.as_hex.lower(),
                f"Mode set to: {mode} via multisig action #{action_id}"
            )

        elif action.action_type == "constitution_update_propose":
            proposal_id_str = params_dict["proposal_id"]
            if proposal_id_str not in self.proposals:
                raise gl.vm.UserError("Referenced proposal does not exist")
            proposal = self.proposals[proposal_id_str]
            if proposal.status != "pending_constitution_confirm":
                raise gl.vm.UserError(
                    "Referenced proposal is not awaiting constitution confirmation"
                )
            self.constitution = proposal.proposed_constitution
            proposal.status = "accepted"
            self._log_governance_change(
                "constitution_confirmed", sender.as_hex.lower(),
                f"Proposal #{proposal_id_str} constitution applied via multisig action #{action_id}"
            )

        self._log_governance_change(
            "admin_action_approved", sender.as_hex.lower(),
            f"Action #{action_id} type:{action.action_type} approved and executed"
        )

    @gl.public.write
    def remove_admin(self, admin_to_remove: str):
        """
        Direct admin removal — only available when there are 3+ admins and
        the requester is an admin removing someone else. For 2-admin setups,
        use propose_admin_action('remove_admin', ...) + approve_admin_action().
        """
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can remove admins")

        target_hex = admin_to_remove.lower()
        if not self.admins.get(target_hex, False):
            raise gl.vm.UserError("Address is not an active admin")
        if self._active_admin_count() <= 1:
            raise gl.vm.UserError("Cannot remove the last admin")
        if self._active_admin_count() == 2:
            raise gl.vm.UserError(
                "With only 2 admins, removal requires 2-admin approval. "
                "Use propose_admin_action('remove_admin', ...) + approve_admin_action()."
            )

        self.admins[target_hex] = False
        self._log_governance_change(
            "admin_removed", sender.as_hex.lower(), f"Removed {target_hex}"
        )

    @gl.public.write
    def set_governance_params(
        self, min_quorum: u256, approval_threshold: u256,
        min_dur: u64, max_dur: u64
    ):
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can set governance parameters")
        if min_quorum == 0:
            raise gl.vm.UserError("min_quorum must be at least 1")
        if approval_threshold == 0 or approval_threshold > 100:
            raise gl.vm.UserError("approval_threshold must be between 1 and 100")
        if min_dur == 0:
            raise gl.vm.UserError("min_voting_duration must be greater than 0")
        if min_dur >= max_dur:
            raise gl.vm.UserError("min_voting_duration must be less than max_voting_duration")

        self.min_quorum                 = min_quorum
        self.approval_threshold_percent = approval_threshold
        self.min_voting_duration        = min_dur
        self.max_voting_duration        = max_dur
        self._log_governance_change(
            "params_updated", sender.as_hex.lower(),
            f"quorum:{min_quorum} threshold:{approval_threshold}% "
            f"duration:{min_dur}s-{max_dur}s"
        )

    @gl.public.write
    def set_safety_params(
        self, max_dispute_stages: u256, cooldown_seconds: u256,
        max_resubmits: u256
    ):
        """Admin-configurable safety parameters (previously hardcoded constants)."""
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can set safety parameters")
        if max_dispute_stages == 0:
            raise gl.vm.UserError("max_dispute_stages must be at least 1")
        if cooldown_seconds == 0:
            raise gl.vm.UserError("cooldown_seconds must be greater than 0")
        if max_resubmits == 0:
            raise gl.vm.UserError("max_resubmits must be at least 1")

        self.max_dispute_stages_count = max_dispute_stages
        self.dispute_cooldown_secs    = cooldown_seconds
        self.max_resubmissions        = max_resubmits
        self._log_governance_change(
            "safety_params_updated", sender.as_hex.lower(),
            f"dispute_stages:{max_dispute_stages} cooldown:{cooldown_seconds}s "
            f"resubmits:{max_resubmits}"
        )

    @gl.public.write
    def set_token_rules(self, token: str, min_vote: u256, min_propose: u256):
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can set token rules")
        self.voting_token          = Address(token)
        self.min_tokens_to_vote    = min_vote
        self.min_tokens_to_propose = min_propose
        self._log_governance_change(
            "token_rules_updated", sender.as_hex.lower(),
            f"token:{token} min_vote:{min_vote} min_propose:{min_propose}"
        )

    @gl.public.write
    def toggle_whitelist(self, vote_wl: bool, propose_wl: bool):
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can toggle whitelist settings")
        self.use_whitelist_for_voting    = vote_wl
        self.use_whitelist_for_proposing = propose_wl
        self._log_governance_change(
            "whitelist_toggled", sender.as_hex.lower(),
            f"voting_whitelist:{vote_wl} proposing_whitelist:{propose_wl}"
        )

    @gl.public.write
    def manage_whitelist(self, user: str, can_vote: bool, can_propose: bool):
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can manage whitelist entries")
        user_hex = user.lower()
        self.allowed_voters[user_hex]    = can_vote
        self.allowed_proposers[user_hex] = can_propose
        self._log_governance_change(
            "whitelist_updated", sender.as_hex.lower(),
            f"user:{user_hex} can_vote:{can_vote} can_propose:{can_propose}"
        )
    @gl.public.write
    def remove_from_whitelist(self, user: str):
        """
        Fully remove an address from both voting and proposing whitelists.
        Equivalent to manage_whitelist(user, False, False) but explicit and clear.
        """
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can manage whitelist entries")
        user_hex = user.lower()
        self.allowed_voters[user_hex]    = False
        self.allowed_proposers[user_hex] = False
        self._log_governance_change(
            "whitelist_removed", sender.as_hex.lower(),
            f"Removed {user_hex} from all whitelists"
        )




    @gl.public.view
    def get_constitution(self) -> str:
        return self.constitution

    @gl.public.write
    def confirm_constitution_update(self, proposal_id: u256):
        """
        Direct single-admin path for constitution updates accepted via dispute resolution.
        For constitution updates accepted via vote, see finalize_decision (automatic).
        For the multisig path, use propose_admin_action('constitution_update_propose', ...).
        """
        sender = gl.message.sender_address
        if not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can confirm constitution updates")

        proposal_id_str = str(proposal_id)
        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        proposal = self.proposals[proposal_id_str]
        if proposal.status != "pending_constitution_confirm":
            raise gl.vm.UserError("Proposal is not awaiting constitution confirmation")
        if proposal.proposal_type != "constitution_update":
            raise gl.vm.UserError("Not a constitution update proposal")

        self.constitution = proposal.proposed_constitution
        proposal.status = "accepted"
        self._log_governance_change(
            "constitution_confirmed", sender.as_hex.lower(),
            f"Proposal #{proposal_id} constitution applied"
        )


    # -----------------------------------------------------------------------
    # Proposals
    # -----------------------------------------------------------------------

    @gl.public.write
    def submit_proposal(
        self, title: str, description: str, voting_duration: u64,
        proposal_type: str = "standard", proposed_constitution: str = ""
    ):
        sender = gl.message.sender_address

        if not title.strip():
            raise gl.vm.UserError("Title cannot be empty")
        if not description.strip():
            raise gl.vm.UserError("Description cannot be empty")
        if voting_duration == 0:
            raise gl.vm.UserError("voting_duration cannot be zero")
        if len(title) > 200 or len(description) > 5000:
            raise gl.vm.UserError("Title or description too long (max: 200 / 5000 characters)")
        if proposal_type not in ["standard", "constitution_update"]:
            raise gl.vm.UserError("Invalid proposal_type. Use 'standard' or 'constitution_update'")
        if proposal_type == "constitution_update" and not proposed_constitution.strip():
            raise gl.vm.UserError("proposed_constitution cannot be empty for constitution updates")
        if proposal_type == "constitution_update" and not self._is_admin(sender):
            raise gl.vm.UserError("Only admins can submit constitution update proposals")

        self._check_proposal_eligibility(sender)

        if voting_duration < self.min_voting_duration or voting_duration > self.max_voting_duration:
            raise gl.vm.UserError(
                f"voting_duration must be between {int(self.min_voting_duration)}s "
                f"and {int(self.max_voting_duration)}s "
                f"({int(self.min_voting_duration) // 3600}h - "
                f"{int(self.max_voting_duration) // 86400}d)"
            )

        constitution_snapshot = self.constitution
        audit_result = self._audit_proposal_logic(
            title, description, constitution_snapshot,
            proposal_type, proposed_constitution
        )

        self.proposal_count += 1
        proposal_id     = self.proposal_count
        proposal_id_str = str(proposal_id)

        decision = audit_result["decision"]
        initial_status = "pending"
        if decision == "reject":
            initial_status = "rejected"
        elif decision == "revise":
            initial_status = "needs_revision"

        now        = _get_now()
        voting_end = u64(int(now) + int(voting_duration))

        proposal = Proposal(
            id=proposal_id,
            proposer=sender.as_hex.lower(),
            title=title,
            description=description,
            proposal_type=proposal_type,
            proposed_constitution=proposed_constitution,
            constitution_snapshot=constitution_snapshot,
            status=initial_status,
            ai_audit_decision=decision,
            ai_audit_reasoning=audit_result["reasoning"],
            submitted_at=now,
            voting_start=now,
            voting_end=voting_end,
            votes_yes=0,
            votes_no=0,
            dispute_count=0,
            last_dispute_at=u64(0),
            resubmit_count=0,
        )

        self.proposals[proposal_id_str] = proposal
        self.votes[proposal_id_str] = gl.storage.inmem_allocate(TreeMap[str, bool])

        # Populate conflicts into the contract-level TreeMap entry.
        # get_or_insert_default auto-creates an empty DynArray if the key is absent,
        # avoiding the KeyError that __getitem__ raises for missing keys.
        for c in audit_result.get("constitutional_conflicts", []):
            if isinstance(c, dict):
                self.proposal_conflicts.get_or_insert_default(proposal_id_str).append(
                    ConstitutionalConflict(
                        clause=c.get("clause", ""),
                        violation=c.get("violation", ""),
                    )
                )

        self._log_governance_change(
            "proposal_created", sender.as_hex.lower(),
            f"Proposal #{proposal_id} (type:{proposal_type} ai:{decision} "
            f"status:{initial_status} closes:{int(voting_end)})"
        )

    @gl.public.write
    def cancel_proposal(self, proposal_id: u256):
        """
        Cancel a proposal before its voting window closes.
        - Proposer can cancel a 'pending' or 'needs_revision' proposal.
        - Admins can cancel any 'pending' or 'needs_revision' proposal.
        Once cancelled, the proposal cannot be revived.
        """
        sender          = gl.message.sender_address
        proposal_id_str = str(proposal_id)

        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        proposal = self.proposals[proposal_id_str]

        if proposal.status not in ["pending", "needs_revision"]:
            raise gl.vm.UserError(
                f"Only 'pending' or 'needs_revision' proposals can be cancelled. "
                f"Current status: {proposal.status}"
            )

        is_proposer = proposal.proposer == sender.as_hex.lower()
        if not is_proposer:
            raise gl.vm.UserError(
                "Only the original proposer can cancel their own proposal"
            )

        proposal.status = "cancelled"
        self._log_governance_change(
            "proposal_cancelled", sender.as_hex.lower(),
            f"Proposal #{proposal_id} cancelled by {sender.as_hex.lower()}"
        )

    @gl.public.write
    def resubmit_proposal(self, proposal_id: u256, new_description: str):
        sender          = gl.message.sender_address
        proposal_id_str = str(proposal_id)

        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        proposal = self.proposals[proposal_id_str]

        if proposal.proposer != sender.as_hex.lower():
            raise gl.vm.UserError("Only the original proposer can resubmit this proposal")
        if proposal.status != "needs_revision":
            raise gl.vm.UserError(
                f"Only 'needs_revision' proposals can be resubmitted. "
                f"Current status: {proposal.status}"
            )
        if not new_description.strip():
            raise gl.vm.UserError("New description cannot be empty")
        if len(new_description) > 5000:
            raise gl.vm.UserError("Description too long (max: 5000 characters)")
        if int(proposal.resubmit_count) >= int(self.max_resubmissions):
            raise gl.vm.UserError(
                f"Maximum resubmissions ({self.max_resubmissions}) reached for this proposal."
            )

        now = _get_now()
        if int(now) > int(proposal.voting_end):
            raise gl.vm.UserError(
                "The voting window has expired. Submit a new proposal instead."
            )

        audit_result = self._audit_proposal_logic(
            proposal.title, new_description, proposal.constitution_snapshot,
            proposal.proposal_type
        )
        decision = audit_result["decision"]

        proposal.description        = new_description
        proposal.ai_audit_decision  = decision
        proposal.ai_audit_reasoning = audit_result["reasoning"]
        proposal.resubmit_count     = u256(int(proposal.resubmit_count) + 1)

        # Clear old conflicts and write fresh ones.
        # DynArray supports len() and pop() — pop from the end until empty.
        conflict_arr = self.proposal_conflicts.get_or_insert_default(proposal_id_str)
        while len(conflict_arr) > 0:
            conflict_arr.pop()
        for c in audit_result.get("constitutional_conflicts", []):
            if isinstance(c, dict):
                conflict_arr.append(ConstitutionalConflict(
                    clause=c.get("clause", ""),
                    violation=c.get("violation", ""),
                ))

        if decision == "accept":
            proposal.status = "pending"
        elif decision == "reject":
            proposal.status = "rejected"
        else:
            proposal.status = "needs_revision"

        self._log_governance_change(
            "proposal_resubmitted", sender.as_hex.lower(),
            f"Proposal #{proposal_id} resubmitted (attempt {int(proposal.resubmit_count)} "
            f"of {self.max_resubmissions}, ai:{decision})"
        )

    def _audit_proposal_logic(
        self,
        title: str,
        description: str,
        constitution: str,
        proposal_type: str = "standard",
        proposed_constitution: str = "",
    ) -> dict:
        def audit_nondet() -> dict:
            if proposal_type == "constitution_update":
                task = f"""
            AUDIT CONSTITUTION AMENDMENT

            You are auditing a proposed amendment to an existing governance constitution.
            Your task is to evaluate whether the proposed amendment is valid, coherent,
            and compatible with the spirit of the current constitution.
            Base your analysis ONLY on the content inside the XML tags.

            <title>{title}</title>
            <rationale>{description}</rationale>
            <current_constitution>{constitution}</current_constitution>
            <proposed_amendment>{proposed_constitution}</proposed_amendment>

            Evaluate the amendment on these criteria:
            1. Is the proposed amendment clearly defined and unambiguous?
            2. Does it conflict with or undermine other clauses in the current constitution?
            3. Is there a reasonable governance rationale provided?
            4. Does it introduce security, fairness, or integrity risks?

            If the amendment is clear, coherent, and does not create contradictions: decision = "accept"
            If it needs minor clarification but is fundamentally sound: decision = "revise"
            If it introduces conflicts, risks, or is fundamentally unsound: decision = "reject"

            Respond ONLY with valid JSON in this exact structure:
            {{
                "decision": "accept" | "reject" | "revise",
                "reasoning": "explanation",
                "constitutional_conflicts": [
                    {{"clause": "clause name", "violation": "reason"}}
                ]
            }}
            """
            else:
                task = f"""
            AUDIT PROPOSAL

            Analyze whether the proposal below violates any clauses of the constitution.
            Base your analysis ONLY on the content inside the XML tags.

            <title>{title}</title>
            <description>{description}</description>
            <constitution>{constitution}</constitution>

            Respond ONLY with valid JSON in this exact structure:
            {{
                "decision": "accept" | "reject" | "revise",
                "reasoning": "explanation",
                "constitutional_conflicts": [
                    {{"clause": "clause name", "violation": "reason"}}
                ]
            }}
            """
            result = gl.nondet.exec_prompt(task, response_format="json")
            return normalize_ai_output(result, context="audit")

        def validate_audit(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                leader_data    = leader_result.calldata
                validator_data = audit_nondet()
                valid_decisions = {"accept", "reject", "revise"}
                return (
                    leader_data.get("decision") in valid_decisions
                    and validator_data.get("decision") in valid_decisions
                    and leader_data["decision"] == validator_data["decision"]
                )
            except Exception:
                return False

        return gl.vm.run_nondet_unsafe(audit_nondet, validate_audit)


    # -----------------------------------------------------------------------
    # Voting
    # -----------------------------------------------------------------------

    @gl.public.write
    def vote(self, proposal_id: u256, support: bool):
        sender          = gl.message.sender_address
        proposal_id_str = str(proposal_id)

        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        self._check_voting_eligibility(sender)

        proposal = self.proposals[proposal_id_str]

        if proposal.status != "pending":
            raise gl.vm.UserError(
                f"Voting is not open for proposals with status: {proposal.status}"
            )

        now = _get_now()
        if int(now) > int(proposal.voting_end):
            raise gl.vm.UserError(
                f"Voting period has closed (closed at Unix {int(proposal.voting_end)})"
            )

        sender_hex = sender.as_hex.lower()
        if sender_hex in self.votes[proposal_id_str]:
            raise gl.vm.UserError("Already voted on this proposal")

        self.votes[proposal_id_str][sender_hex] = support
        if support:
            proposal.votes_yes += 1
        else:
            proposal.votes_no += 1


    # -----------------------------------------------------------------------
    # Finalization
    # -----------------------------------------------------------------------

    @gl.public.write
    def finalize_decision(self, proposal_id: u256):
        proposal_id_str = str(proposal_id)

        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        proposal = self.proposals[proposal_id_str]

        if proposal.status != "pending":
            raise gl.vm.UserError("Proposal is not in a finalizable state")

        now = _get_now()
        if int(now) <= int(proposal.voting_end):
            raise gl.vm.UserError(
                f"Voting period has not yet closed "
                f"(closes at Unix {int(proposal.voting_end)})"
            )

        total_votes = proposal.votes_yes + proposal.votes_no

        if total_votes < self.min_quorum:
            proposal.status = "rejected"
            proposal.ai_audit_reasoning += (
                f" | REJECTED: Quorum not reached "
                f"({int(total_votes)} votes, required {int(self.min_quorum)})."
            )
            return

        approval_ratio = (proposal.votes_yes * 100) // total_votes

        if (
            approval_ratio >= self.approval_threshold_percent
            and proposal.ai_audit_decision == "accept"
        ):
            if proposal.proposal_type == "constitution_update":
                # Constitution updates always require explicit admin confirmation
                # before the constitution is overwritten, regardless of whether
                # acceptance came via vote or dispute resolution.
                # Call confirm_constitution_update() or approve_admin_action() next.
                proposal.status = "pending_constitution_confirm"
                self._log_governance_change(
                    "constitution_update_pending_confirm", proposal.proposer,
                    f"Proposal #{proposal_id} vote-accepted; awaiting admin confirmation"
                )
            else:
                proposal.status = "accepted"
        else:
            proposal.status = "rejected"
            if approval_ratio < self.approval_threshold_percent:
                proposal.ai_audit_reasoning += (
                    f" | REJECTED: Approval threshold not met "
                    f"({int(approval_ratio)}% < {int(self.approval_threshold_percent)}%)."
                )
            elif proposal.ai_audit_decision != "accept":
                proposal.ai_audit_reasoning += (
                    f" | REJECTED: Vote threshold met ({int(approval_ratio)}%) but "
                    f"AI audit decision was '{proposal.ai_audit_decision}'. "
                    "Raise a dispute to challenge the audit outcome."
                )


    # -----------------------------------------------------------------------
    # Dispute Resolution
    # -----------------------------------------------------------------------

    @gl.public.write
    def raise_dispute(self, proposal_id: u256, reason: str):
        sender          = gl.message.sender_address
        proposal_id_str = str(proposal_id)

        if proposal_id_str not in self.proposals:
            raise gl.vm.UserError("Proposal does not exist")

        proposal = self.proposals[proposal_id_str]

        is_proposer = proposal.proposer == sender.as_hex.lower()
        is_voter    = sender.as_hex.lower() in self.votes[proposal_id_str]
        if not is_proposer and not is_voter and not self._is_admin(sender):
            raise gl.vm.UserError(
                "Only the proposer, a voter on this proposal, or an admin can raise a dispute"
            )

        if proposal.status not in ["rejected", "accepted"]:
            raise gl.vm.UserError(
                f"Disputes can only be raised on finalized proposals "
                f"(rejected or accepted). Current status: {proposal.status}"
            )

        if int(proposal.dispute_count) >= int(self.max_dispute_stages_count):
            raise gl.vm.UserError(
                f"Maximum dispute stages ({self.max_dispute_stages_count}) "
                "already reached for this proposal"
            )

        if not reason.strip():
            raise gl.vm.UserError("Dispute reason cannot be empty")

        now = _get_now()
        cooldown = int(self.dispute_cooldown_secs)
        if int(proposal.last_dispute_at) != 0 and int(now) - int(proposal.last_dispute_at) < cooldown:
            cooldown_ends = int(proposal.last_dispute_at) + cooldown
            raise gl.vm.UserError(
                f"Dispute cooldown active. Next dispute allowed after Unix {cooldown_ends} "
                f"({cooldown // 3600}h cooldown between stages)."
            )

        proposal.status = "under_review"
        proposal.dispute_count += 1
        stage = int(proposal.dispute_count)

        title_mem       = proposal.title
        description_mem = proposal.description
        snapshot_mem    = proposal.constitution_snapshot

        def resolve_dispute_nondet() -> dict:
            prompt_complexity = (
                "Perform a scrupulous re-evaluation of the original audit."
                if stage == 1
                else "Simulate a panel of 3 independent legal AI agents and provide a final consensus decision."
            )
            task = f"""
            DISPUTE RESOLUTION - STAGE {stage}

            {prompt_complexity}

            Evaluate ONLY the content inside the XML tags below.

            <proposal_id>{proposal_id}</proposal_id>
            <title>{title_mem}</title>
            <description>{description_mem}</description>
            <constitution>{snapshot_mem}</constitution>
            <dispute_reason>{reason}</dispute_reason>

            Respond ONLY with valid JSON in this exact structure:
            {{
                "decision": "accept" | "reject",
                "reasoning": "comprehensive resolution explanation"
            }}
            """
            result = gl.nondet.exec_prompt(task, response_format="json")
            return normalize_ai_output(result, context="dispute")

        def validate_dispute(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                leader_data    = leader_result.calldata
                validator_data = resolve_dispute_nondet()
                valid_decisions = {"accept", "reject"}
                return (
                    leader_data.get("decision") in valid_decisions
                    and validator_data.get("decision") in valid_decisions
                    and leader_data["decision"] == validator_data["decision"]
                )
            except Exception:
                return False

        resolution = gl.vm.run_nondet_unsafe(resolve_dispute_nondet, validate_dispute)

        proposal.last_dispute_at = now
        self.proposal_disputes.get_or_insert_default(proposal_id_str).append(DisputeRecord(
            stage=u32(stage),
            reason=reason,
            resolution=resolution["reasoning"],
            timestamp=now,
        ))

        dispute_decision = resolution["decision"]
        proposal.ai_audit_reasoning = (
            f"DISPUTE STAGE {stage} RESOLVED ({dispute_decision.upper()}): "
            f"{resolution['reasoning']}"
        )

        if dispute_decision == "accept":
            if proposal.proposal_type == "constitution_update":
                proposal.status = "pending_constitution_confirm"
                self._log_governance_change(
                    "constitution_update_pending_confirm", proposal.proposer,
                    f"Proposal #{proposal_id} dispute-accepted; awaiting admin confirmation"
                )
            else:
                proposal.status = "accepted"
                self._log_governance_change(
                    "dispute_resolved_accepted", proposal.proposer,
                    f"Proposal #{proposal_id} accepted at dispute stage {stage}"
                )
        else:
            proposal.status = "rejected"
            self._log_governance_change(
                "dispute_resolved_rejected", proposal.proposer,
                f"Proposal #{proposal_id} rejected at dispute stage {stage}"
            )


    # -----------------------------------------------------------------------
    # Views
    # -----------------------------------------------------------------------

    @gl.public.view
    def get_proposal(self, proposal_id: u256) -> dict:
        proposal_id_str = str(proposal_id)
        if proposal_id_str not in self.proposals:
            return {}
        p = self.proposals[proposal_id_str]

        conflicts = []
        if proposal_id_str in self.proposal_conflicts:
            conflicts = [
                {"clause": c.clause, "violation": c.violation}
                for c in self.proposal_conflicts[proposal_id_str]
            ]
        history = []
        if proposal_id_str in self.proposal_disputes:
            history = [
                {
                    "stage": int(h.stage), "reason": h.reason,
                    "resolution": h.resolution, "timestamp": int(h.timestamp)
                }
                for h in self.proposal_disputes[proposal_id_str]
            ]

        return {
            "id":                       int(p.id),
            "proposer":                 p.proposer,
            "title":                    p.title,
            "description":              p.description,
            "proposal_type":            p.proposal_type,
            "proposed_constitution":    p.proposed_constitution,
            "constitution_snapshot":    p.constitution_snapshot,
            "status":                   p.status,
            "ai_audit_decision":        p.ai_audit_decision,
            "ai_audit_reasoning":       p.ai_audit_reasoning,
            "constitutional_conflicts": conflicts,
            "submitted_at":             int(p.submitted_at),
            "voting_start":             int(p.voting_start),
            "voting_end":               int(p.voting_end),
            "votes_yes":                int(p.votes_yes),
            "votes_no":                 int(p.votes_no),
            "dispute_count":            int(p.dispute_count),
            "dispute_history":          history,
            "last_dispute_at":          int(p.last_dispute_at),
            "resubmit_count":           int(p.resubmit_count),
        }

    @gl.public.view
    def get_governance_history(self, offset: u256, limit: u256) -> typing.List[dict]:
        """
        Paginated governance history. offset is 0-based.
        get_governance_history(0, 20) -> first 20 entries
        get_governance_history(20, 20) -> next 20 entries
        """
        if limit == 0:
            raise gl.vm.UserError("limit must be greater than 0")
        total = int(self.governance_history_count)
        start = int(offset)
        end   = min(start + int(limit), total)
        result = []
        for i in range(start, end):
            c = self.governance_history[i]
            result.append({
                "change_type": c.change_type,
                "actor":       c.actor,
                "details":     c.details,
                "timestamp":   int(c.timestamp),
            })
        return result

    @gl.public.view
    def get_pending_actions(self) -> typing.List[dict]:
        result = []
        count  = int(self.pending_action_count)
        for i in range(1, count + 1):
            action_id = str(i)
            if action_id not in self.pending_actions:
                continue
            a = self.pending_actions[action_id]
            result.append({
                "action_id":   a.action_id,
                "action_type": a.action_type,
                "params":      a.params,
                "proposer":    a.proposer,
                "proposed_at": int(a.proposed_at),
                "status":      a.status,
            })
        return result

    @gl.public.view
    def get_admins(self) -> typing.List[str]:
        seen   = {}
        result = []
        for a_hex in self.admin_list:
            if a_hex not in seen and self.admins.get(a_hex, False):
                result.append(a_hex)
                seen[a_hex] = True
        return result

    @gl.public.view
    def get_whitelist(self) -> dict:
        """
        Returns all addresses currently on either whitelist.
        Each entry shows whether the address can_vote and/or can_propose.
        Only entries that are True in at least one whitelist are returned.
        """
        combined: dict = {}
        for addr_hex, can_vote in self.allowed_voters.items():
            if can_vote:
                if addr_hex not in combined:
                    combined[addr_hex] = {"can_vote": False, "can_propose": False}
                combined[addr_hex]["can_vote"] = True
        for addr_hex, can_propose in self.allowed_proposers.items():
            if can_propose:
                if addr_hex not in combined:
                    combined[addr_hex] = {"can_vote": False, "can_propose": False}
                combined[addr_hex]["can_propose"] = True
        return combined

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "min_quorum":                  int(self.min_quorum),
            "approval_threshold_percent":  int(self.approval_threshold_percent),
            "min_voting_duration":         int(self.min_voting_duration),
            "max_voting_duration":         int(self.max_voting_duration),
            "eligibility_mode":            self.eligibility_mode,
            "voting_token":                self.voting_token.as_hex,
            "min_tokens_to_vote":          int(self.min_tokens_to_vote),
            "min_tokens_to_propose":       int(self.min_tokens_to_propose),
            "use_whitelist_for_voting":    self.use_whitelist_for_voting,
            "use_whitelist_for_proposing": self.use_whitelist_for_proposing,
            "active_admin_count":          self._active_admin_count(),
            "max_admins":                  int(self.max_admins),
            "max_dispute_stages":          int(self.max_dispute_stages_count),
            "dispute_cooldown_seconds":    int(self.dispute_cooldown_secs),
            "max_resubmissions":           int(self.max_resubmissions),
            "proposal_count":              int(self.proposal_count),
            "governance_history_count":    int(self.governance_history_count),
        }

    @gl.public.view
    def list_proposals(self, offset: u256, limit: u256) -> typing.List[dict]:
        """
        Paginated proposal listing. offset is 1-based.
        list_proposals(1, 20)  -> proposals 1-20
        list_proposals(21, 20) -> proposals 21-40
        """
        if limit == 0:
            raise gl.vm.UserError("limit must be greater than 0")
        start = int(offset)
        end   = min(start + int(limit), int(self.proposal_count) + 1)
        return [self.get_proposal(u256(i)) for i in range(start, end)]
