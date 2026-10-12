# Jev experiment on hold

The maintainer deferred Jev adoption on 2026-10-12 after live experiments did not
meet the fixed qualification requirements. Version 1.4.0 removes the experimental
review command, provider runtime, configuration example and shared authoring gate.
This note is historical evidence, not an installation or authoring workflow.

The qualification bar required all 16 planted critical gaps to be detected,
at least 15 of 16 supported implementations recognized, all eight missing-proof
cases inconclusive, no unsupported completion/defect conclusions, at most one
false defect, and no unassessed cases. Preserved Choice runs fell short on clean
implementations or encountered invalid service responses. One complete revised
run detected all 16 critical gaps but supported only 13 of 16 clean cases.

A subsequent paired yes/no diagnostic used six opened synthetic cases and a fixed
binary confidence floor with conservative scalar guard. The first formulation
left all six inconclusive. A reviewed serialization correction detected one of
two planted gaps, supported neither clean implementation and correctly abstained
on both missing-proof cases. Both rounds produced structurally valid responses;
neither established feasibility or earned fresh qualification credit.

The detailed fixed reports and source freezes are preserved in the separate
experiment worktree and its ignored assessment workspace. No raw model payloads,
credentials or customer data are stored in this repository. Draft experiment PR44
is superseded by removal, not promoted to a release. Any future experiment needs
a separately approved design, fresh independent labels and qualification before
consumer adoption. Deterministic handoff enforcement never depended on Jev.
