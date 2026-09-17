"""Learning loop, Stage 2: threshold proposals from operator decisions.

Proposals are computed from approved scans only and shown in the Learning panel. Nothing is applied until the
operator presses Apply, which stores a SettingOverride (with the previous value, for rollback) and updates settings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlmodel import Session, select

from banana.config import Settings
from banana.core.corrections import training_events
from banana.models import SettingOverride, utcnow

MIN_EVIDENCE = 5


@dataclass
class Proposal:
    key: str
    label: str
    current: float | int
    proposed: float | int | None
    evidence: dict
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def blank_back_proposal(session: Session, settings: Settings) -> Proposal:
    current = settings.analysis.blank_edge_density
    blanks, contents = [], []
    for event in training_events(session, "blank_back"):
        density = (event.asset_ref or {}).get("edge_density")
        if density is None:
            continue
        (blanks if event.approved == "blank" else contents).append(float(density))
    evidence = {"confirmed_blank": len(blanks), "confirmed_content": len(contents),
                "wrongly_blank": sum(1 for d in contents if d < current), "wrongly_content": sum(1 for d in blanks if d >= current)}
    proposal = Proposal("analysis.blank_edge_density", "Blank-back threshold", current, None, evidence, "")
    if len(blanks) < MIN_EVIDENCE or len(contents) < MIN_EVIDENCE:
        proposal.reason = f"Needs at least {MIN_EVIDENCE} approved blank and {MIN_EVIDENCE} approved non-blank backs."
        return proposal
    top_blank, low_content = max(blanks), min(contents)
    if top_blank < low_content:
        proposed = round((top_blank + low_content) / 2, 6)
        proposal.reason = f"Every approved blank back is below {low_content:.6f} and every kept back above {top_blank:.6f}."
    else:  # overlap: pick the value with the fewest mistakes
        candidates = sorted(set(blanks + contents))
        proposed = min(candidates, key=lambda t: sum(d >= t for d in blanks) + sum(d < t for d in contents))
        proposal.reason = "Approved blank and non-blank backs overlap; this value makes the fewest mistakes."
    if evidence["wrongly_blank"] + evidence["wrongly_content"] == 0 and abs(proposed - current) < 1e-6:
        proposal.reason = "The current threshold already matches every decision."
        return proposal
    proposal.proposed = proposed
    return proposal


def duplicate_proposal(session: Session, settings: Settings) -> Proposal:
    current = settings.analysis.dhash_max_distance
    events = training_events(session, "duplicate")
    false_alarms = [
        int((e.asset_ref or {}).get("distance"))
        for e in events
        if e.action == "removed" and (e.asset_ref or {}).get("distance") is not None
    ]
    confirmed = [int((e.asset_ref or {}).get("distance")) for e in events
                 if e.action == "kept" and (e.asset_ref or {}).get("distance") is not None]
    evidence = {"flag_was_wrong": len(false_alarms), "flag_confirmed": len(confirmed)}
    proposal = Proposal("analysis.dhash_max_distance", "Duplicate (rescan) sensitivity", current, None, evidence, "")
    if len(false_alarms) < MIN_EVIDENCE:
        proposal.reason = f"Needs at least {MIN_EVIDENCE} approved scans whose duplicate flag you cleared."
        return proposal
    closest_false = min(false_alarms)
    farthest_real = max(confirmed, default=-1)
    candidate = closest_false - 1
    if farthest_real <= candidate < current:
        proposal.proposed = candidate
        proposal.reason = (f"{len(false_alarms)} flags were wrong, the closest at distance {closest_false}; "
                           f"confirmed duplicates reach distance {max(farthest_real, 0)}.")
    else:
        proposal.reason = "Wrong and confirmed duplicate flags overlap; no safe change."
    return proposal


def all_proposals(session: Session, settings: Settings) -> list[Proposal]:
    return [blank_back_proposal(session, settings), duplicate_proposal(session, settings)]


def apply_overrides(session: Session, settings: Settings) -> None:
    for override in session.exec(select(SettingOverride)):
        section, name = override.key.split(".", 1)
        setattr(getattr(settings, section), name, override.value)


def apply_proposal(session: Session, settings: Settings, key: str) -> SettingOverride:
    proposal = next((p for p in all_proposals(session, settings) if p.key == key), None)
    if proposal is None or proposal.proposed is None:
        raise ValueError(f"no applicable proposal for {key}")
    section, name = key.split(".", 1)
    override = session.get(SettingOverride, key) or SettingOverride(key=key)
    override.previous = getattr(getattr(settings, section), name)
    override.value = proposal.proposed
    override.evidence = {**proposal.evidence, "reason": proposal.reason}
    override.applied_at = utcnow()
    session.add(override)
    session.commit()
    setattr(getattr(settings, section), name, proposal.proposed)
    return override


def revert_override(session: Session, settings: Settings, key: str) -> None:
    override = session.get(SettingOverride, key)
    if override is None:
        raise ValueError(f"no override for {key}")
    section, name = key.split(".", 1)
    setattr(getattr(settings, section), name, override.previous)
    session.delete(override)
    session.commit()
