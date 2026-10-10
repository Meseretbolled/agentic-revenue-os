from revos.agents import (
    call_intel,
    contract_io,
    enrichment,
    knowledge,
    meeting,
    outreach,
    qualification,
    reply,
    scoring,
    tracking,
)
from revos.agents.types import AgentSpec

D, J = "deterministic", "judgement"

AGENTS: dict[str, AgentSpec] = {s.name: s for s in [
    AgentSpec("enrichment", D, "Fill in a sparse lead from the company's own site, with cited facts.",
              enrichment.run),
    AgentSpec("scoring", D, "Score fit, intent and engagement by the pack's rules.", scoring.run),
    AgentSpec("outreach", J, "Draft the first touch or a follow-up from cited facts.", outreach.run,
              "You write one short, factual personalised line for a sales email."),
    AgentSpec("reply", J, "Classify a customer reply and route it.", reply.run,
              "You classify customer replies to sales emails."),
    AgentSpec("meeting", J, "Offer meeting times and write a call brief citing lessons.", meeting.run,
              "You write one short line for a meeting-offer email."),
    AgentSpec("call_intel", J, "Turn a call transcript into deal fields with quotes.",
              call_intel.run, "You extract facts from sales call transcripts."),
    AgentSpec("qualification", D, "Check the exit criteria for qualified.", qualification.run),
    AgentSpec("contract_io", D, "Build and validate the IO from deal data.", contract_io.run),
    AgentSpec("tracking", D, "Flag deals that stalled past their SLA.", tracking.run),
    AgentSpec("knowledge", J, "Turn quoted statements into deduplicated lessons.", knowledge.run,
              "You write concise, reusable sales lessons."),
]}
