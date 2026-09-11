# Offline formal-entry fixture

These scripts are test tools, not a production scheduler. They only write the
explicit evidence directory. Never point them at an existing story project.
They use synthetic images/audio/video as upstream provider responses and run the
actual deterministic entries, QA and independent-review gates. Native ImageGen
and RVM response fields in marked fixture receipts are simulated input contracts,
not evidence of real service execution. No paid request is made.

Run with the bundled Python interpreter (numpy/Pillow/python-docx required):

1. `prepare_formal_media.py EVIDENCE_DIR` — confirmed timeline, synthetic provider
   clip, real R2V QA, semantic-card review bundles. Obtain independent reviews at
   both request paths before continuing.
2. The initial preparation now creates valid Word/requirements before review.
   `complete_fixture_inputs.py` is only the retained repair tool for the initial
   experiment; fresh runs do not need it.
3. `continue_formal_media.py EVIDENCE_DIR` — actual formal assembly and customer
   backgrounds at 1920×1080. No hand-authored QA or assembly receipt.
4. `prepare_keying_fixture.py EVIDENCE_DIR OFFICIAL_ONNX_PATH RUNTIME_PATH` —
   synthetic precomputed VP9 Alpha, real keying evidence/QA. Obtain independent
   synthetic-Alpha review; this does not test neural inference.
5. `prepare_media_preview.py EVIDENCE_DIR` — actual keying lock and Demo preview;
   obtain independent preview review.
6. `prepare_panels_fixture.py EVIDENCE_DIR` — four simulated upstream panels and
   actual compiled scope prompts; obtain independent panel review.
7. `continue_media_render.py EVIDENCE_DIR` — real approval, Demo/A-only encoding
   and customer-media QA receipt.
8. `release_fixture.py EVIDENCE_DIR preview` — actual native-v2 dual release
   preview. Obtain independent review of the generated geometry plus frames.
9. `release_fixture.py EVIDENCE_DIR render` — actual dual-account encoding and
   native release manifest. Delivery/final review/seal are separate checks.

The initial failed small-background subtitle QA, rejected panel iteration and
thin-frame evidence are retained in the evidence directory. Final success must
be based on the current outputs and real validators, not this checklist or the
presence of files. See the delivered e2e result record for completed coverage.

10. `audit_fixture.py EVIDENCE_DIR` — real final release machine QA, including
    Demo formal frames. A nonzero result must be resolved, never replaced with a
    hand-written pass receipt.
11. `prepare_materials_fixture.py` prepares mock upstream assets; follow its
    explicit independent review requests and compile/export the actual directory.
12. `prepare_delivery_fixture.py EVIDENCE_DIR` — real managed directory packaging
    and registration, then director/theme review requests. Independent completion
    is required before final checklist/review/finalization.

Recovery note: an earlier isolated run reached dual encoding and directory
packaging, but its release audio-fit QA was run against videos encoded before the
fixture mix changed. The fixture must use the same formal path as production:
authoritative narration in ``--audio-mix``, the music-only customer background as
``--bg-video``, and ``--mix-bg-audio``. Any changed preview, release, QA, review,
or delivery binding must be regenerated in dependency order. Do not treat unit
tests as replacing these gates. Source bundle restoration is a separate,
deterministic verification from story-run checkpoint recovery.
