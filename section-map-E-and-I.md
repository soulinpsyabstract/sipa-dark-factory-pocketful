# Section map: named tests, so the gate verifies against tests and not prose

Core's item 4. Prose descriptions of a section can be wrong in ways nobody
notices until a gate measures against them; a test name cannot drift from the
behaviour it pins. Each leg below is a test name, so the gate runs those names.

Regenerate the lists with:

```
$ grep -h "^def test_" stage-2/tests/test_stage2_void.py
$ grep -h "^def test_" stage-2/tests/test_stage2_authorizations.py
$ grep -h "^def test_" stage-2/tests/test_stage2_capture.py
$ grep -h "^def test_" stage-2/tests/test_stage2_importexport.py
```

## §E - authorization lifecycle

Source: `stage-2/tests/test_stage2_authorizations.py` (47 tests),
`stage-2/tests/test_stage2_void.py` (19 tests), `stage-2/tests/test_stage2_capture.py`.

Hold accumulation and exact-boundary spending:

- test_creating_two_authorizations_accumulates_the_hold
- test_exact_available_is_allowed
- test_a_restored_hold_still_blocks_spending
- test_an_open_hold_survives_export_and_import

Listing and pagination:

- test_list_pagination_limit_offset_and_has_more
- test_an_unfiltered_list_is_never_truncated

TTL:

- TTL positive-int and default-600 cases in test_stage2_authorizations.py

Void legs, all in `test_stage2_void.py`:

- test_void_releases_the_whole_hold
- test_void_moves_no_money_to_the_receiver
- test_void_returns_the_authorization
- test_voiding_twice_is_200_with_the_current_state
- test_voiding_twice_does_not_release_twice
- test_void_needs_no_idempotency_key_and_ignores_one
- test_void_requires_authentication
- test_the_receiver_cannot_void
- test_a_bystander_cannot_void
- test_a_forbidden_void_releases_nothing
- test_a_fully_captured_authorization_cannot_be_voided
- test_an_expired_authorization_cannot_be_voided
- test_voiding_an_unknown_authorization_is_404
- test_void_after_a_partial_capture_releases_only_the_remainder
- test_capture_after_void_is_409
- test_a_voided_authorization_is_listed_as_voided
- test_a_voided_authorization_releases_its_hold_for_spending
- test_void_writes_no_activity
- test_void_preserves_every_invariant

Capture legs:

- test_a_voided_authorization_stays_voided
- test_capture_history_survives

Note on `already_voided`: `SPEC.md:328` requires a double void to answer `200`
with the current state. `already_voided` is an **additive advisory field** on top
of that spec-conformant response, permitted by the vocabulary ruling. It is
implementation at `stage-2/app/routers/authorizations.py`, pinned by
`test_voiding_twice_is_200_with_the_current_state`. **Do not remove it to match
the prose in any gate document.**

## §I - import and export

Source: `stage-2/tests/test_stage2_importexport.py`.

Export carries no credentials:

- test_the_export_carries_no_tokens_and_no_bearer_token_anywhere
- test_a_payload_tokens_key_is_ignored_entirely
- test_a_payload_tokens_key_cannot_mint_a_session_for_another_user
- test_an_old_export_carrying_tokens_cannot_wipe_sessions

A structurally malformed `tokens` key is refused with `422`, and the refusal is
atomic. This replaced `test_a_structurally_broken_tokens_key_is_ignored_not_refused`,
which asserted the opposite rule and was deleted at `0f73ecb`:

- test_a_structurally_malformed_tokens_key_is_refused
- test_refusing_a_malformed_tokens_key_changes_nothing

Sessions survive, and are dropped for deprovisioned handles:

- test_a_stage1_shaped_export_preserves_live_sessions
- test_every_session_survives_not_just_one
- test_a_signed_in_browser_stays_signed_in_across_import
- test_a_live_session_for_a_user_the_import_removes_is_dropped
- test_a_live_session_for_a_removed_user_is_dropped_from_a_stage1_shaped_payload
- test_a_deprovisioning_import_still_signs_the_removed_user_out

Round-trip fidelity:

- test_export_import_export_reaches_a_fixed_point
- test_export_import_export_round_trips_each_field
- test_importing_the_same_export_twice_never_releases_a_hold

Rejections are atomic - the property that would have caught W5:

- test_a_rejected_import_changes_nothing
- test_a_rejected_import_leaves_the_session_working
- test_a_rejected_import_leaves_the_world_usable
- test_a_rejected_import_leaves_sessions_and_authorizations_byte_identical
- test_a_rejected_amount_import_changes_nothing

Imported amounts are validated with the write path's `Amount` type (strict int,
`gt=0`, `le=MAX_AMOUNT`):

- test_an_imported_request_amount_is_validated_like_the_write_path
- test_an_imported_negative_request_amount_cannot_be_paid_backwards
- test_an_imported_negative_authorization_amount_is_refused
- test_an_imported_activity_amount_is_validated
- test_activity_entries_without_an_amount_still_import
- test_negative_balances_are_still_rejected_and_this_did_not_loosen_them

Malformed payloads:

- test_a_non_object_payload_is_refused
- test_a_payload_with_nothing_recognisable_is_refused
- test_an_authorization_with_an_unknown_status_is_refused
- test_an_authorization_naming_an_absent_user_is_refused

## Explicitly tested gate leg: stage-1 snapshot releases holds

Core ruled this correct behaviour, not a leak, and it is a leg rather than prose.

- test_a_stage1_export_imports
- test_a_stage1_export_leaves_no_holds_behind

Holds are domain state carried by the payload; sessions are not. A stage-1
snapshot has no `authorizations` because the feature did not exist, so importing
one restores a world with no holds. Preserving live holds for an absent key would
make restoring a snapshot with *fewer* authorizations impossible.
`SPEC.md:150-151` singles sessions out for survival and they are absent from the
payload entirely, which is the opposite case.

The W5 defect is different and remains pinned:
`test_an_open_hold_survives_export_and_import` covers the case where the key is
**present** and carrying the authorization.