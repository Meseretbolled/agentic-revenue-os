"""Seed a fictional lead. Demo leads are plus-addresses of the owner's one prospect inbox
(name+acme@gmail.com), so each is a distinct HubSpot contact (HubSpot merges contacts by email and
refuses .test and .example domains) while every email still lands in the same inbox."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import yaml
from psycopg.types.json import Jsonb

from revos.policy.recipients import base_of
from revos.ports.clock import Clock
from revos.runtime.events import record_event

PROFILES_FILE = Path(__file__).resolve().parent / "fixtures" / "seed_profiles.yaml"
REAL_PROFILES_FILE = Path(__file__).resolve().parent / "fixtures" / "real_seed_profiles.yaml"
PROFILE_NAMES = ("hot", "warm", "cold")
FALLBACK_BASE = "prospect@example.com"          # used only when PROSPECT_TEST_EMAIL is not set (mock runs)


def load_profiles(segment_id: str, path: Path = PROFILES_FILE) -> dict[str, dict[str, Any]]:
    """The seed profiles for one segment, from the YAML fixture (the data lives there, not in code)."""
    data = yaml.safe_load(path.read_text()) or {}
    profiles = data.get(segment_id)
    if not profiles:
        raise ValueError(f"no seed profiles for segment {segment_id!r} in {path.name}")
    for name, profile in profiles.items():
        missing = {"company", "domain", "contact", "fields"} - set(profile)
        if missing:
            raise ValueError(f"seed profile {name!r} lacks {', '.join(sorted(missing))}")
        if bool(profile.get("signer_name")) != bool(profile.get("signer_role")):
            raise ValueError(f"seed profile {name!r}: signer_name and signer_role go together")
        for note in profile.get("research_notes", []):
            if not str(note.get("text", "")).strip() or not str(note.get("source", "")).lower().startswith(
                    "fictional demo profile"):
                raise ValueError(f"seed profile {name!r}: every research note needs text and a source "
                                 "starting 'fictional demo profile'")
    return profiles


def real_profile(name: str, path: Path = REAL_PROFILES_FILE) -> tuple[str, dict[str, Any]]:
    """(segment id, profile) for a REAL company that is researched on the web (ENRICHMENT_MODE=tavily).
    The company is real; the contact is not: every profile's contact is the owner's own allowlisted inbox
    (a plus-address), never anyone at the company, and nothing is ever sent to the company's domain."""
    data = yaml.safe_load(path.read_text()) or {}
    for segment_id, profiles in data.items():
        if name in profiles:
            profile = profiles[name]
            missing = {"company", "domain", "contact", "fields", "email_tag"} - set(profile)
            if missing:
                raise ValueError(f"real profile {name!r} lacks {', '.join(sorted(missing))}")
            return segment_id, profile
    raise ValueError(f"no real profile {name!r} in {path.name}; have: "
                     + ", ".join(sorted(n for v in data.values() for n in v)))


def demo_address(tag: str, env: Mapping[str, str] | None = None) -> str:
    """The plus-address for a demo lead, e.g. demo_address('acme') -> name+acme@gmail.com."""
    env = os.environ if env is None else env
    base = base_of(env.get("PROSPECT_TEST_EMAIL", "").strip()) or FALLBACK_BASE
    clean = "".join(c for c in tag.lower() if c.isalnum() or c in "._-").strip("._-")
    if not clean:
        raise ValueError("the lead tag needs at least one letter or digit")
    local, _, domain = base.partition("@")
    return f"{local}+{clean[:40]}@{domain}"


def seed_lead(conn: psycopg.Connection, clock: Clock, *, segment_id: str, pack_version: str, company: str,
              domain: str, contact_name: str, email: str, fields: dict | None = None,
              timezone: str | None = None) -> tuple[UUID, bool]:
    """Create the company, contact and deal and queue lead.created. Safe to repeat: the same email
    returns the same deal and queues nothing new. Returns (deal id, created)."""
    email = email.strip().lower()
    if email.endswith("@" + domain.lower().removeprefix("www.")) or email.endswith("." + domain.lower()):
        raise ValueError(f"refusing to seed a contact at the company's own domain ({domain}): demo mail "
                         "goes only to the owner's allowlisted inbox")
    with conn.transaction():
        existing = conn.execute(
            "SELECT d.id FROM deals d JOIN contacts c ON c.id = d.contact_id WHERE lower(c.email) = %s "
            "ORDER BY d.created_at LIMIT 1", (email,)).fetchone()
        if existing:
            return existing[0], False
        conn.execute("INSERT INTO segments (id, pack_version) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (segment_id, pack_version))
        company_id = conn.execute(
            "INSERT INTO companies (segment_id, name, domain) VALUES (%s, %s, %s) RETURNING id",
            (segment_id, company, domain)).fetchone()[0]
        contact_id = conn.execute(
            "INSERT INTO contacts (company_id, email, full_name, timezone) VALUES (%s, %s, %s, %s) "
            "RETURNING id", (company_id, email, contact_name, timezone)).fetchone()[0]
        deal_id = conn.execute(
            "INSERT INTO deals (segment_id, company_id, contact_id, fields) VALUES (%s, %s, %s, %s) "
            "RETURNING id", (segment_id, company_id, contact_id, Jsonb(fields or {}))).fetchone()[0]
    record_event(conn, f"lead.created:{deal_id}", "lead.created", {"deal_id": str(deal_id)}, clock)
    return deal_id, True
