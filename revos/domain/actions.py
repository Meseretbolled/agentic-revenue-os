# Every action key a pack must give an autonomy level to (action types, plus one entry per
# first-touch tier and the price-mention rule). A test keeps this equal to the policy defaults.
KNOWN_ACTION_KEYS = frozenset({
    "enrich", "score", "crm_update", "crm_note",
    "first_touch.A", "first_touch.B", "first_touch.C",
    "follow_up", "reminder", "classify_reply", "snooze_ooo",
    "suppress_contact", "cancel_scheduled_sends",
    "attach_case_study", "generate_io", "send_io", "social_message", "linkedin_draft", "lesson_write",
    "book_meeting", "handoff",
    "price_message",
})
