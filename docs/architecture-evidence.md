# Architecture evidence

One row per box in the diagrams of `docs/DESIGN.md`: the code that implements it and a test that proves it. Run any test with `.venv/bin/python -m pytest <path>::<name>`. A test name starting with `::` is in the same file as the one before it in that row.


## Fig 1a: how an event becomes a decision

| Box | Code | Test |
|---|---|---|
| Webhooks (Slack, Dropbox Sign) | revos/api/main.py (`/slack/interactions`, `/dropboxsign/webhook`, tunnel allowlist) | tests/integration/test_public_routes.py::test_through_the_tunnel_the_webhooks_and_health_answer; tests/integration/test_slack.py::test_a_bad_stale_or_missing_signature_is_a_401_and_changes_nothing |
| Scheduler (6 recurring events) | revos/runtime/scheduler.py (`DEFAULT_SCHEDULES`) | tests/integration/test_timers_scheduler.py::test_scheduler_twice_in_the_same_bucket_creates_no_duplicates; tests/integration/test_run.py::test_the_scheduler_enqueues_each_recurring_event_once_even_with_two_schedulers |
| CLI (seed, run, linkedin draft) | revos/cli/main.py | tests/integration/test_run.py::test_seeding_is_idempotent_and_each_plus_address_is_its_own_contact; tests/integration/test_linkedin.py::test_the_draft_is_made_once_per_deal |
| Events + queue (unique key, leases, backoff, dead letters) | revos/runtime/events.py, revos/runtime/queue.py, revos/runtime/worker.py | tests/integration/test_events_queue.py::test_duplicate_event_is_a_noop; ::test_job_is_reclaimed_after_the_lease_expires; ::test_backoff_grows_exponentially_with_jitter; ::test_retries_exhaust_into_the_dead_letter_table |
| Orchestrator (14 job handlers, stage machine, exit criteria) | revos/orchestrator/pipeline.py (`Orchestrator.handlers`), revos/orchestrator/transitions.py | tests/integration/test_run.py::test_the_recurring_jobs_all_have_handlers_and_run_without_dead_letters; tests/integration/test_slice.py::test_the_full_slice_runs_offline_through_the_orchestrator |
| 11 agents (5 rules-based, 6 model-judged) | revos/agents/registry.py (`AGENTS`) | tests/integration/test_agents_flow.py::test_all_eleven_agents_exist_with_the_declared_kinds; tests/unit/test_agent_architecture.py::test_the_agent_environment_holds_no_way_to_act |
| 18 skill modules | revos/skills/*.py (18 modules besides `__init__`) | tests/unit/test_claims.py, tests/unit/test_scoring.py, tests/unit/test_drafting.py, tests/unit/test_io_terms.py |
| 10 read-only tools, scoped per agent | revos/mcp/agents.py (`REGISTRY`, `AGENT_SCOPES`), revos/mcp/tools_*.py | tests/integration/test_mcp_scope.py::test_every_declared_tool_exists_and_every_tool_has_an_owner; ::test_a_tool_outside_the_agents_scope_is_refused |
| Claude Agent SDK (Sonnet, Haiku) | revos/adapters/claude_llm.py, revos/agents/runner.py (`SdkRunner`), revos/composition.py (`build_runner`) | tests/integration/test_sdk_runner.py::test_the_sdk_gets_no_builtin_tools_no_host_settings_and_only_the_agents_own_tools; tests/integration/test_claude_llm.py::test_spend_cap_blocks_the_call_that_would_pass_it |
| Segment packs (2 packs, YAML) | packs/franchise_brands, packs/agencies, revos/segments/schema.py | tests/unit/test_packs.py::test_real_pack_loads_with_everything_in_it; tests/unit/test_no_segment_names.py::test_no_python_file_names_a_segment |
| Lessons store (tag + keyword) | revos/mcp/tools_lessons.py, revos/runtime/lessons.py, migrations/0001_init.sql, 0006_lessons_loop.sql | tests/integration/test_mcp_scope.py::test_lessons_rank_by_tag_then_keywords; tests/integration/test_lessons_loop.py::test_lessons_come_from_the_call_with_their_evidence_source_and_segment |
| Web research (mock default, Tavily opt-in) | revos/adapters/mock/research.py (default); revos/adapters/tavily/research.py (`TavilyResearch`), revos/skills/entity_match.py, revos/skills/web_research.py, revos/composition.py (`build_research`) | tests/unit/test_enrichment_setup.py::test_the_default_is_the_mock_and_the_mock_object_is_returned_unchanged; tests/unit/test_entity_match.py::test_the_known_wrong_company_case_unrelated_real_companies_are_all_rejected; tests/integration/test_web_enrichment.py::test_every_rejected_result_is_audited_with_its_reason; tests/unit/test_superlatives.py::test_an_unattributed_superlative_from_a_web_fact_is_rejected; tests/unit/test_recorded_live_research.py::test_no_wrong_company_result_is_accepted_and_replay_costs_nothing |
| Postgres (all state, audit log) | migrations/0001_init.sql to 0015_dead_letter_ack.sql, revos/db.py | tests/integration/test_migrations.py::test_migrate_is_idempotent; tests/integration/test_transitions.py::test_audit_log_is_append_only |

## Fig 1b: how a decision becomes a side effect

| Box | Code | Test |
|---|---|---|
| Agent proposal (cited facts) | revos/agents/types.py (`Proposal`), revos/agents/common.py (`make`) | tests/unit/test_agent_architecture.py::test_a_proposal_is_plain_data; ::test_only_the_orchestrator_applies_proposals_and_moves_stages |
| Policy engine (allowlist, autonomy mode, hard blocks) | revos/policy/engine.py (`decide`), revos/policy/service.py (`propose_action`), revos/policy/config.py | tests/unit/test_policy_engine.py::test_unknown_action_type_is_denied; ::test_a_hard_block_beats_every_autonomy_level; tests/integration/test_policy_service.py::test_every_decision_is_audited_with_its_reasons |
| Veto window (60 s, Slack Cancel, atomic) | revos/policy/service.py, revos/slack/interactions.py, revos/slack/notifier.py | tests/integration/test_slack.py::test_cancel_at_second_59_wins_and_at_second_60_loses; ::test_letting_the_window_expire_sends_exactly_once |
| Approval queue (Slack, dashboard) | revos/policy/service.py (human approval), revos/slack/interactions.py, revos/dashboard/actions.py | tests/integration/test_policy_service.py::test_human_approval_records_who_and_when; ::test_a_hard_blocked_action_cannot_be_approved_by_a_person |
| Draft only (LinkedIn, social, no provider) | revos/policy/config.py (`DRAFT_ONLY_TYPES`), revos/agents/linkedin.py, revos/skills/linkedin.py | tests/integration/test_linkedin.py::test_nothing_in_the_code_base_can_send_to_linkedin; tests/integration/test_policy_service.py::test_social_message_is_only_ever_a_draft |
| Send gate (kill switch, DB re-read, allowlist, window, caps) | revos/policy/gate.py (`send_with_gate`), revos/policy/windows.py, revos/policy/recipients.py | tests/integration/test_send_gate.py::test_caller_flags_in_the_payload_do_not_approve_anything; ::test_only_allowed_recipients_can_be_emailed_in_demo_mode; ::test_two_concurrent_sends_at_the_limit_exactly_one_passes; tests/integration/test_kill_switch_resume.py::test_switch_on_three_sends_wait_switch_off_exactly_three_sends_happen_once_each |
| Outbox (unique key, find, then perform) | revos/runtime/outbox.py (`execute_action`) | tests/integration/test_outbox.py::test_retry_after_crash_checks_the_provider_and_does_not_resend; tests/integration/test_outbox_guards.py::test_idempotency_key_is_unique |
| Gmail (SMTP/IMAP, REAL) | revos/adapters/gmail/mailer.py, inbox.py | tests/integration/test_gmail_loop.py::test_a_crash_after_gmail_accepted_the_mail_is_recovered_by_find_sent_not_resent |
| HubSpot (CRM, REAL) | revos/adapters/hubspot/crm.py | tests/integration/test_hubspot_outbox.py::test_a_crash_after_hubspot_saved_the_record_does_not_create_a_second_one |
| Slack (handoff, REAL) | revos/adapters/slack/handoff.py, client.py | tests/unit/test_slack_units.py::test_post_update_and_ephemeral_use_the_right_methods_and_never_show_the_token; tests/integration/test_esign_flow.py::test_all_signed_closes_the_deal_once_updates_hubspot_and_posts_the_handoff |
| Dropbox Sign (TEST MODE) | revos/adapters/dropboxsign/client.py, revos/adapters/esign_provider.py | tests/unit/test_dropboxsign_adapter.py::test_a_request_with_test_mode_off_is_refused_before_any_network_call; tests/integration/test_esign_flow.py::test_a_lost_create_response_is_recovered_from_the_provider_and_never_sent_twice |
| Calendar (invite, MOCK) | revos/adapters/mock/calendar.py (wired in revos/composition.py as `world.calendar`) | tests/integration/test_close_the_deal.py::test_an_accept_slot_reply_books_the_meeting_once_and_sends_an_invite |
| Audit log (append-only, every decision) | migrations/0001_init.sql (`audit_log_no_change`), revos/policy/audit.py | tests/integration/test_transitions.py::test_audit_log_is_append_only; tests/integration/test_policy_service.py::test_every_decision_is_audited_with_its_reasons |

## Fig 2: stage machine (one row per box)

| Box | Code | Test |
|---|---|---|
| new, enriched, scored, outreach, replied, meeting, qualified, proposal, io_approval, out_for_signature, closed_won, handoff | revos/domain/stages.py (`ALLOWED_TRANSITIONS`), revos/orchestrator/transitions.py (`transition_deal`), packs/*/stages.yaml (`exit_criteria`) | tests/unit/test_stages.py::test_happy_path_is_allowed_step_by_step; tests/integration/test_transitions.py::test_exit_criteria_block_the_move_until_fields_exist; tests/integration/test_exit_criteria_from_pack.py::test_pack_exit_criteria_gate_the_qualified_stage |
| replied to outreach (follow-up) and io_approval to proposal (revise) loops | revos/domain/stages.py (`_FORWARD`) | tests/unit/test_stages.py::test_skips_and_backwards_moves_are_refused |
| closed_lost, do_not_contact (reachable from any live stage) | revos/domain/stages.py (`ALLOWED_TRANSITIONS`) | tests/unit/test_stages.py::test_do_not_contact_reachable_from_every_live_stage; ::test_terminal_stages_have_no_exit |
| Direct stage updates refused | migrations/0001_init.sql (`deals_stage_guard`) | tests/integration/test_transitions.py::test_direct_stage_update_is_refused_by_the_database |

## Fig 3: data model (one row per table in the figure)

| Table | Migration | Test |
|---|---|---|
| `segments` | migrations/0001_init.sql | tests/integration/test_outbox_guards.py (via the `conn` fixture in tests/conftest.py) |
| `companies` | migrations/0001_init.sql | tests/integration/test_slice.py::test_the_full_slice_runs_offline_through_the_orchestrator |
| `contacts` | migrations/0001_init.sql | tests/integration/test_outbox_guards.py::test_send_to_suppressed_contact_is_refused |
| `deals` | migrations/0001_init.sql | tests/integration/test_transitions.py::test_legal_transition_updates_stage_version_and_audit |
| `actions` | migrations/0001_init.sql, 0002_policy.sql | tests/integration/test_outbox_guards.py::test_sent_without_a_decision_is_refused |
| `messages` | migrations/0001_init.sql, 0007_reply_matching.sql | tests/integration/test_gmail_loop.py::test_a_reply_is_found_matched_to_its_deal_handled_and_marked_delivered_once |
| `transcripts` | migrations/0001_init.sql | tests/integration/test_calls.py::test_a_finished_call_fills_the_fields_each_with_a_quote_that_is_really_in_the_transcript |
| `documents` | migrations/0001_init.sql | tests/integration/test_close_the_deal.py::test_a_valid_io_is_built_validated_approved_and_sent_for_signature_in_test_mode |
| `lessons` | migrations/0001_init.sql, 0006_lessons_loop.sql | tests/integration/test_lessons_loop.py::test_lessons_come_from_the_call_with_their_evidence_source_and_segment |
| `lesson_uses` | migrations/0001_init.sql, 0006_lessons_loop.sql | tests/integration/test_lessons_loop.py::test_using_a_lesson_is_counted_once_when_the_email_goes_out |
| `case_studies` | migrations/0001_init.sql, 0005, 0014 | tests/integration/test_case_studies.py::test_the_library_holds_exactly_the_four_studies_with_their_urls_tags_and_labels |
| `case_study_sends` | migrations/0001_init.sql, 0005 | tests/integration/test_replies.py::test_the_same_case_study_is_never_sent_to_a_deal_twice |
| `events` | migrations/0001_init.sql, 0008_correlation.sql | tests/integration/test_events_queue.py::test_duplicate_event_is_a_noop |
| `jobs` | migrations/0001_init.sql | tests/integration/test_events_queue.py::test_concurrent_workers_never_share_a_job |
| `dead_letters` | migrations/0001_init.sql, 0015_dead_letter_ack.sql | tests/integration/test_events_queue.py::test_dead_letter_can_be_retried_and_then_succeeds |
| `audit_log` | migrations/0001_init.sql, 0008 | tests/integration/test_transitions.py::test_audit_log_is_append_only |
| `llm_spend` | migrations/0003_llm_spend.sql, 0012_llm_tokens.sql | tests/integration/test_claude_llm.py::test_spend_cap_blocks_the_call_that_would_pass_it |

Tables in the schema but not drawn: approvals, agent_runs, agent_steps, agent_proposals, held_replies, held_calls, esign_attempts, send_counters, system_settings, slack_messages, slack_interactions. There is no separate outbox, timer, IO-terms or signature-event table (see the Fig 3 caption).


## What was verified live and what was not

Tavily was run LIVE once on 2026-10-12 for Orangetheory Fitness and Hawke Media (2 searches, 2 extracts, 2 credits reported); the real answers are recorded in `revos/fixtures/tavily_recorded_live_2026-10-12/` and replayed by `tests/unit/test_recorded_live_research.py`. Everything else in the web-enrichment path (caps, fallback, injection, citation, never-send) is proven offline with synthetic fixtures. Not verified live: Tavily's plan limits, a search that returns an instruction-like page, and a failing key.

## What the evidence does not cover

The other real-service behaviour (HubSpot, Gmail, Slack, Fireflies, Dropbox Sign test mode, live Claude) is recorded in `docs/INTEGRATIONS.md`; the tests here run offline against fakes and mocks and a real Postgres.
