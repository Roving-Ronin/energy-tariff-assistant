"""Build a pre-filled GitHub "Plan submission" link for a plan.

Nothing is sent from Home Assistant: the user opens the link, checks the
form on GitHub and submits it from their own account. Only the plan goes in
the link (rates, times, retailer, region); never meter totals, sensor names
or anything else from the entry. No Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlencode

from .const import CONF_LIBRARY_FILE, CONF_SOURCE, SOURCE_LIBRARY

ISSUE_FORM_URL = "https://github.com/Roving-Ronin/energy-tariff-assistant/issues/new"
TEMPLATE = "plan_submission.yml"
# GitHub refuses (414) or truncates very long URLs; stay well under ~8 KB.
MAX_URL_LENGTH = 7500
STATES = ("NSW", "ACT", "VIC", "QLD", "SA", "TAS", "WA", "NT")

# Option labels of the form's "New or correction?" dropdown
KIND_NEW = "New plan"
KIND_CORRECTION = "Correction to a library plan"


@dataclass(frozen=True)
class ShareLink:
    """The link, and whether the plan JSON fitted in it."""

    url: str
    plan_in_link: bool
    plan_json: str


def plan_title(plan: dict[str, Any]) -> str:
    dnsp = f", {plan['dnsp']}" if plan.get("dnsp") else ""
    return f"[Plan] {plan.get('retailer', '?')} {plan.get('plan', '?')} ({plan.get('region', '?')}{dnsp})"


def build_share_link(
    plan: dict[str, Any], source: dict[str, Any] | None, integration_version: str
) -> ShareLink:
    """A link to the plan submission form with its fields filled in.

    The plan JSON goes in the link when it fits; otherwise the link carries
    everything else and the user pastes the JSON (shown to them) themselves.
    """
    source = source or {}
    from_library = source.get(CONF_SOURCE) == SOURCE_LIBRARY
    plan_text = json.dumps(plan, indent=2, ensure_ascii=False)

    plan_id = plan.get("plan_id") or "unknown"
    fields: dict[str, str] = {
        "template": TEMPLATE,
        "title": plan_title(plan),
        "kind": KIND_CORRECTION if from_library else KIND_NEW,
        "retailer-plan": f"{plan.get('retailer', '')} {plan.get('plan', '')}".strip(),
        "dnsp": plan.get("dnsp") or "",
        "plan-id": f"{plan_id}, from {plan.get('effective_from', '?')}",
        "rates": "See the plan JSON below.",
        "source": _source_text(plan, source, integration_version),
    }
    if plan.get("region") in STATES:
        fields["state"] = plan["region"]

    with_plan = _url({**fields, "plan-json": plan_text})
    if len(with_plan) <= MAX_URL_LENGTH:
        return ShareLink(with_plan, True, plan_text)
    fields["rates"] = "Plan JSON too long for the link: pasted below by hand."
    return ShareLink(_url(fields), False, plan_text)


def _source_text(plan: dict[str, Any], source: dict[str, Any], version: str) -> str:
    parts = [f"Shared from Home Assistant (Energy Tariff Assistant {version})"]
    if source.get(CONF_SOURCE) == SOURCE_LIBRARY and source.get(CONF_LIBRARY_FILE):
        parts.append(f"library file {source[CONF_LIBRARY_FILE]}")
    if url := (plan.get("source") or {}).get("url"):
        parts.append(str(url))
    return "; ".join(parts)


def _url(fields: dict[str, str]) -> str:
    return f"{ISSUE_FORM_URL}?{urlencode(fields)}"
