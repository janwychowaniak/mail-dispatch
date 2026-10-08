# Specification coverage

Every acceptance case of [`SPEC.md`](SPEC.md) §10.2, with the tests that prove it. Case numbers
are the specification's and never shift. A case without a test is a promise nobody checks.

Every case in which a message reaches the fake server ends with an assertion on the raw
message it recorded, parsed independently with the standard library (§10.1). Tests named
`test_case_N_…` are the acceptance tests proper, through HTTP and the fake. The unit tests
listed beside them prove the same rule at the level of the module.

| Case | Subject | Tests |
| --- | --- | --- |
| 1 | `text` only, its encoding, `Date`, `Message-ID`, `size_bytes`, dot transparency | `test_send.py::test_case_1_text_only`, `::test_case_1_non_ascii_text_is_base64`; `test_compose.py::test_text_only`, `::test_content_without_a_final_line_break_reads_back_without_one`; `test_encoding.py::test_encoding_rule_counts_bytes_outside_32_126`, `::test_stuffing_piece_by_piece_equals_stuffing_the_whole` |
| 2 | `html`; `html` + `inline` as `multipart/related` | `test_send.py::test_case_2_html_only`, `::test_case_2_html_with_inline`; `test_compose.py::test_html_with_inline` |
| 3 | the full structure, attachment hashes, guessed types, `boundary` refused | `test_send.py::test_case_3_full_structure`, `::test_case_3_boundary_parameter_is_refused`; `test_compose.py::test_full_structure` |
| 4 | RFC 2047 subjects and names, RFC 2231 file names, 1000-character subject and name | `test_send.py::test_case_4_encoded_headers`, `::test_case_4_unfoldable_subject`; `test_compose.py::test_encoded_subject_and_names`, `::test_filenames_in_rfc_2231`, `::test_unfoldable_ascii_subject_and_name_are_encoded`; `test_encoding.py::test_encoded_words_decode_to_the_text`, `::test_long_filenames_use_continuations_within_78` |
| 5 | `bcc` only in the envelope, no fabricated `To`, `reply_to`, sending to oneself | `test_send.py::test_case_5_envelope_and_headers`; `test_compose.py::test_envelope_only_bcc_and_no_fabricated_to` |
| 6 | custom headers verbatim and folded, refusals, header order | `test_send.py::test_case_6_custom_headers`, `::test_case_6_header_refusals`, `::test_case_6_unfoldable_cid`; `test_validation.py::test_custom_header_errors`; `test_compose.py::test_custom_headers_and_order` |
| 7 | control characters → `INVALID_HEADER`, zero connections | `test_send.py::test_case_7_control_characters`; `test_validation.py::test_control_characters_are_invalid_header`, `::test_control_characters_come_before_grammars`, `::test_a_name_of_a_single_tab_is_a_control_character` |
| 8 | address grammar, duplicates, request shape | `test_send.py::test_case_8_address_grammar`, `::test_case_8_invalid_requests`, `::test_case_8_repeated_key_and_lone_surrogate`; `test_addresses.py`; `test_validation.py::test_shape_errors`, `::test_duplicate_recipient_points_at_the_second_occurrence` |
| 9 | `INVALID_CONTENT`; `report..pdf` accepted | `test_send.py::test_case_9_invalid_content`, `::test_case_9_two_dots_inside_a_name`; `test_validation.py::test_part_errors` |
| 10 | limits and their envelopes; the server's `SIZE` with and without a fresh probe | `test_send.py::test_case_10_request_too_large`, `::test_case_10_message_too_large`, `::test_case_10_too_many_recipients`, `::test_case_10_server_size_after_a_fresh_probe`, `::test_case_10_server_size_without_a_fresh_probe`; `test_envelope.py::test_body_over_the_limit_without_content_length`, `::test_content_type_is_checked_before_the_size` |
| 11 | partial acceptance, verbatim replies, the rejected `cc` still in `Cc` | `test_send.py::test_case_11_partial_acceptance` |
| 12 | every recipient refused, no `DATA`; 5xx at each stage | `test_send.py::test_case_12_all_rejected`, `::test_case_12_rejected_at_each_stage` |
| 13 | transient refusals, multi-line greeting, 5xx to `EHLO`, 4xx to `STARTTLS` | `test_send.py::test_case_13_transient`, `::test_case_13_first_transient_reply_is_reported`, `::test_case_13_multi_line_greeting`, `::test_case_13_ehlo_refused_without_helo`, `::test_case_13_starttls_refused` |
| 14 | unreachable, unresolvable, closed before the greeting, broken during and after the content | `test_transport.py::test_case_14_*` |
| 15 | silence after `EHLO`, no greeting | `test_transport.py::test_case_15_silence` |
| 16 | the TLS modes, untrusted and trusted certificates, implicit TLS | `test_transport.py::test_case_16_*`, `::test_certificate_for_another_name`, `::test_starttls_never_falls_back_after_a_failed_handshake` |
| 17 | authentication: after STARTTLS, `PLAIN` before `LOGIN`, refusals, never without TLS | `test_transport.py::test_case_17_*`, `::test_auth_login_out_of_course`, `::test_the_password_is_utf8`; `test_settings.py::test_none_with_credentials_does_not_start` |
| 18 | health: one probe per TTL, capabilities, `down`, `tls_failed`, no `AUTH` or `MAIL FROM` | `test_health.py::test_case_18_*`, `::test_the_probe_has_one_deadline`, `::test_concurrent_requests_share_one_probe` |
| 19 | determinism | `test_send.py::test_case_19_determinism`; `test_compose.py::test_two_messages_differ_only_in_date_id_and_boundaries` |
| 20 | no traffic but the server; nothing written to disk | the autouse fixtures `connections` and `_no_writes` in `conftest.py`, around every test; `test_send.py::test_case_20_only_the_fake_is_reached`; the `test-in-image` CI job (read-only, no network) |
| 21 | the envelope always; an exception on both sides of the final write | `test_boundaries.py::test_case_21a_*`, `::test_case_21b_*`, `::test_case_21c_*`, `::test_case_21d_*`, `::test_the_stage_is_data_end_when_the_final_write_is_issued`; `test_envelope.py::test_unknown_path_is_not_found`, `::test_method_not_allowed_names_the_allowed_one`, `::test_an_escaped_exception_is_enveloped`; `test_health.py::test_an_exception_in_health_is_enveloped` |
| 22 | the HTTP client goes away before and after the final write | `test_boundaries.py::test_case_22a_client_gone_before_the_final_write`, `::test_case_22b_client_gone_after_the_final_write`, `::test_client_gone_before_the_connection_is_opened` |

Beyond the numbered cases:

| Requirement | Tests |
| --- | --- |
| §10.3 robustness: mutated requests | `test_robustness.py` |
| §10.1 read-only file system, proven twice [D46] | the `test-in-image` CI job (`--read-only --tmpfs /tmp --network none`) and the `image` CI job (`--read-only`, no tmpfs) |
| §7 configuration and startup validation [D28] | `test_settings.py`; `test_logging.py::test_invalid_configuration_stops_startup`; the `image` CI job |
| §9 logging [D30] | `test_logging.py`; `test_send.py::test_logs_carry_no_text_and_no_content`; `test_transport.py::test_case_17_wrong_password` |
| §3 content type, paths and methods | `test_envelope.py` |
