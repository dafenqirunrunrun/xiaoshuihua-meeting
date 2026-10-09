"""水华队长 report schemas (ADR-002).

Two shapes live here and the distinction matters, exactly as in AGENT-1:

  `CaptainReportContent`  the LLM-WRITABLE substance: the referee's report.
                          This is the parse gate AND the prompt's declared
                          output contract — one object, so the two cannot drift.

  `CaptainReport`         the DURABLE record: content + the pipeline-owned
                          scalars (`status`, `anchor_segment_seq`, window
                          bounds, `model_name`). Those scalars are deliberately
                          ABSENT from `CaptainReportContent`, so the model
                          structurally cannot forge its own window bounds or
                          promote itself to COMPLETED.

Opinion model (ADR-002 §4)
--------------------------
There is no `side_a` / `side_b`. The referee discovers 1..N POSITIONS from the
transcript itself. Two reasons this is a schema-level decision, not a prompt
preference:

  1. `speaker_id` is not a reliable identity in this system. Product speaker
     labels are disabled by default (`ASR_SPEAKER_LABELS_ENABLED=false`) and the
     partial path pins `speaker_id` to -1, so "A side vs B side" would invite the
     model to attribute claims to people it cannot actually identify.
  2. A meeting is not a debate. Forcing exactly two sides fabricates a conflict
     whenever the discussion is exploratory, one-sided, or already aligned.

So a report with ONE position and an empty `dispute_points` is a VALID and
common outcome — see `CaptainReportContent`. The referee must be free to say
"no disagreement here".

Evidence (ADR-002 §5)
---------------------
Every claim carries `source_segment_seqs`, and `evidence` additionally carries
verbatim `quote`s. Both are re-validated server-side against the persisted
transcript by `captain/evidence.py`; the schema alone cannot prove a citation
is real.

Two-stage assessment (Task 3)
-----------------------------
`QuickAssessment` and `DeepAssessment` describe the SAME window at two
resolutions, and they are TYPES rather than a flat bag of fields because the
speed guarantee has to be structural. The Quick call's latency target is met by
giving it four small fields with a small token budget — a Quick result physically
cannot become a slow summary, because there is no field for a summary in it.
`DeepAssessment` then carries everything the first pass could not afford.

`CaptainReportContent.quick` / `.deep` are OPTIONAL. A report produced before
this existed, or by the single-pass provider, validates with both absent and the
flat fields populated; `with_assessments` is the one place the Deep output is
mirrored into the flat fields the validator and the UI already read.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The referee's evidence-strength vocabulary. A closed set with an ordered,
# named scale, deliberately NOT a percentage.
#
# The spec this implements is explicit that "60% / 40%" style scoring is
# forbidden: a number implies a measurement that does not exist here (the
# referee has no denominator — it read a few minutes of ASR text), while a named
# level with a `basis` forces the same information to be stated as a REASON,
# which is the thing a reader can actually check against the transcript.
EvidenceStrengthLevel = Literal["低", "中", "中高", "高"]

# What KIND of source a piece of external evidence is (Phase C.5).
#
# Separate axis from `source_quality`, and the separation is load-bearing:
# `source_type` is an OBSERVABLE property of the URL (a vendor's docs host is
# documentation regardless of who reads it), whereas `source_quality` is the
# trust tier THIS system assigns. A marketing page and an API reference can both
# be `source_type="documentation"` on the same host yet differ in whether they
# are a statement about behaviour — which is precisely the distinction a reader
# needs and a single field would erase.
SourceType = Literal[
    "official",
    "documentation",
    "standard",
    "paper",
    "media",
    "community",
    "other",
]

SOURCE_TYPE_LEVELS: tuple[str, ...] = (
    "official",
    "documentation",
    "standard",
    "paper",
    "media",
    "community",
    "other",
)

# How a source bears on the claim it was fetched to answer (Phase C.5).
#
# `contextual` is the default and the honest one. A search provider returns a
# snippet that MENTIONS the topic; very few results actually assert the claim or
# its negation, and forcing a support/contradict split onto a snippet that does
# neither manufactures a stance nobody took. When the relation is not evident
# from the snippet, `contextual` is the correct answer, not a fallback failure.
EvidenceRelation = Literal["support", "contradict", "contextual"]

EVIDENCE_RELATIONS: tuple[str, ...] = ("support", "contradict", "contextual")

# Machine-readable outcome of the external-research stage (Phase C.5 §7).
#
# Replaces reliance on a prose `note` folded into `uncertainty`, which a
# consumer could not act on. The prose note is KEPT (a human still needs the
# sentence) but it is no longer the only signal: a client can now render
# "外部资料检索超时" from `research_status` without string-matching Chinese text.
#
#   not_required  the router decided no external facts were needed
#   success       research completed and produced usable evidence
#   partial       some queries/sources failed, but usable evidence survived
#   timeout       research was cut off by its budget
#   failed        research did not produce usable evidence at all
ResearchStatus = Literal[
    "not_required",
    "success",
    "partial",
    "timeout",
    "failed",
]

RESEARCH_STATUSES: tuple[str, ...] = (
    "not_required",
    "success",
    "partial",
    "timeout",
    "failed",
)


class Position(BaseModel):
    """One viewpoint cluster discovered in the window.

    Deliberately NOT a person. `label` is a short neutral handle ("成本优先"),
    `statement` is the position as an argued claim. Attribution to a speaker is
    out of scope: the transcript does not carry a trustworthy identity.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=60)
    statement: str = Field(..., min_length=1, max_length=1000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class PointItem(BaseModel):
    """A shared agreement or a live disagreement, with its grounding."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=1000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """A verbatim quotation from the transcript, and what it supports.

    `quote` MUST be a substring of the referenced segment's text. The model is
    asked for it and `captain/evidence.py` ENFORCES it: a quote that does not
    appear in the cited segment is dropped, because a paraphrase presented as a
    quotation is the most convincing form of a hallucination.

    This is TRANSCRIPT evidence only. It is validated against persisted
    segments by `captain/evidence.py` and is the only evidence type that carries
    a `segment_seq`. See `ExternalEvidence` for the web-sourced counterpart.
    """

    model_config = ConfigDict(extra="forbid")

    segment_seq: int = Field(..., ge=1)
    quote: str = Field(..., min_length=1, max_length=1000)
    supports: str = Field(..., min_length=1, max_length=300)


class ExternalEvidence(BaseModel):
    """One web source that the referee used as outside information (Phase 3-2).

    Structurally SEPARATE from `EvidenceItem`, and the separation is the point:

      * There is no `segment_seq` here and there never will be. A web page is
        not a transcript segment; giving it a segment number would make it
        indistinguishable from meeting content to every downstream reader,
        including the citation validator.
      * `source_id` is a LOCAL handle (`web_1`, `web_2`, …) scoped to one
        report, used so the model can refer to a source in `uncertainty` without
        pasting a URL.
      * This is NOT validated by `captain/evidence.py`. There is nothing to
        validate against: the source is whatever the search provider returned,
        and the provider — not this system — is the authority on it. What IS
        enforced is that the model cannot fabricate one, because entries are
        written by the PIPELINE from real provider results, not by the model.

    `url` is retained for display and auditability even though the frontend does
    not yet render it.

    Grounding limit (Phase C.5) — what `evidence_summary` MAY and MAY NOT claim
    --------------------------------------------------------------------------
    Every field here is derived from what the SEARCH PROVIDER returned: its
    title, its pre-extracted snippet, its URL. Phase 3-2 §6 and Phase C.5 both
    forbid fetching the page ourselves, so this system has never seen the page
    body. `evidence_summary` is therefore **search-result-level grounding**, NOT
    full-page verification, and nothing here may be presented to a reader as
    "we checked the page and it says X".

    Concretely: the model may be asked to RESTATE the provider's snippet, and
    that restatement is checked to be a faithful compression of the snippet. It
    is never asked for a date, a figure, or a claim that is not in the snippet,
    because it has no source for one. `published_at` stays empty rather than
    being guessed — an invented publication date is a fabricated credential.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=32)
    title: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=2000)
    # The provider's extracted passage. Already length-capped at the boundary.
    context: str = Field(default="", max_length=2000)
    domain: str = Field(default="", max_length=255)
    retrieved_at: str = ""
    provider: str = Field(default="", max_length=64)
    # Authority class assigned from the host by `search/research.py::quality_for`.
    # One of official / standard / reputable / secondary, or "" when unknown.
    # A HEURISTIC and documented as one: this system cannot verify a web page, so
    # the honest field is where the page came from, not a verdict on it.
    source_quality: str = Field(default="", max_length=32)
    # Local handle of the claim this source was fetched to answer (`claim_1`).
    # Set by the pipeline from the research plan, so a reader can see WHICH
    # meeting claim a source addresses rather than treating it as ambient fact.
    answers_question: str = Field(default="", max_length=32)

    # --- Phase C.5 additions ------------------------------------------------
    # ALL of these have defaults, which is the backward-compatibility contract:
    # a report_json written before Phase C.5 has none of them and must still
    # validate. `extra="forbid"` is what makes that a real constraint rather
    # than a formality — adding a REQUIRED field here would fail every old row
    # through `to_public()`, which degrades a readable report to FAILED.

    # What KIND of source this is (as opposed to how much the system trusts it).
    # `source_type` and `source_quality` are deliberately separate axes:
    #   source_type    = what the thing IS (official docs? a blog? a paper?)
    #   source_quality = the tier this system assigns it
    # A vendor's marketing page and its API reference are both
    # `source_type="official"` yet only one is a `source_quality="official"`
    # statement about behaviour. Collapsing the two would lose exactly that.
    source_type: str = Field(default="", max_length=32)
    # How this source bears on the claim it answers. One of
    # support / contradict / contextual, or "" when the pipeline did not
    # classify it. `contextual` is the HONEST DEFAULT for a snippet that merely
    # mentions the topic: most search results do not actually settle a dispute,
    # and forcing a support/contradict split onto them manufactures a stance the
    # provider never asserted.
    relation: str = Field(default="", max_length=16)
    # The research question this result came back for, as actually sent to the
    # provider. Distinct from `answers_question`, which is the claim HANDLE:
    # the handle says WHICH meeting claim, the query says WHAT WAS ASKED. Both
    # are needed to audit a judgement — a source answering claim_1 via the wrong
    # query is a research bug the handle alone would hide.
    query: str = Field(default="", max_length=300)
    # A one-or-two sentence compression of `context` (the provider snippet).
    # NOT a summary of the page. See the grounding note in this docstring.
    evidence_summary: str = Field(default="", max_length=1000)
    # Publication date as the PROVIDER reported it, or "" when it did not.
    # Never inferred, never parsed out of a URL, never guessed by a model: an
    # invented date is a fabricated credential that a reader cannot detect.
    published_at: str = Field(default="", max_length=64)


class AssuranceGap(BaseModel):
    """One fact the referee treats as established but that it could not verify.

    Separate from `missing_evidence` on purpose, and the difference is
    epistemic, not cosmetic:

      `missing_evidence`   something NOBODY provided, which would settle the
                           dispute outright. ("没有压测的环境准备时长说明")
      `assumptions`        something the referee itself leaned on to keep
                           reasoning, which no participant actually said.
                           ("假定下周五之后仍有发布会窗口")

    Merging them lets the report blame the participants for a gap the referee
    introduced. Keeping them apart is what allows the reader to discount the
    second without discounting the first.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=1000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class EvidenceStrength(BaseModel):
    """How strong the evidence behind ONE claim is, and why.

    `claim` names the claim by its position label so the reader can match it to
    `claims[]`; `level` is the ordered vocabulary and `basis` is REQUIRED — a
    level with no stated reason is exactly the arbitrary grading this schema
    exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(..., min_length=1, max_length=200)
    level: EvidenceStrengthLevel
    basis: str = Field(..., min_length=1, max_length=1000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class QuickAssessment(BaseModel):
    """The 1-2 second first pass: enough to orient the room, nothing more.

    Written by a SEPARATE, budgeted call whose entire output is small by
    construction — the latency target is met by the schema, not by hoping the
    model is brief. Four fields:

      issue                 what is being disputed, in one sentence
      key_evidence          the 1-3 transcript citations that matter most
      provisional_judgement a first read of where the dispute actually is.
                            "provisional" is load-bearing: the Deep pass is
                            allowed to move this, and must say so when it does.
      question_to_resolve   the single question whose answer would move things

    There is no summary field and no agreement/dispute split here: those belong
    to the observer (`meeting_state`) and to the Deep pass respectively. A Quick
    result that grew them would be a summary, at speed, which is the failure
    mode the two-stage design exists to avoid.
    """

    model_config = ConfigDict(extra="forbid")

    issue: str = Field(..., min_length=1, max_length=500)
    key_evidence: list[EvidenceItem] = Field(default_factory=list, max_length=3)
    provisional_judgement: str = Field(..., min_length=1, max_length=1000)
    question_to_resolve: str = Field(..., min_length=1, max_length=500)


class DeepAssessment(BaseModel):
    """The full judgement: what the Quick pass could not fit in its budget.

    The field list is the spec's referee output, verbatim in intent. Two
    structural properties matter:

      1. `confirmed_facts` / `assumptions` / `missing_evidence` are THREE lists,
         not one "notes" field. The reader must be able to tell "the meeting
         established this" from "the model needed this" from "nobody knows
         this", and a single free-text field collapses them the first time a
         model is in a hurry.
      2. `supersedes` is where a reversal is declared. The Deep pass is
         explicitly ALLOWED to correct the Quick pass, but never silently: the
         prompt requires the reversal to be stated, and this field is where it
         is stated. Absent means "the first read held up".
    """

    model_config = ConfigDict(extra="forbid")

    issue: str = Field(..., min_length=1, max_length=1000)
    # Capped at 5 to match `CaptainReportContent.positions`, which the assembly
    # step feeds from here. The two limits MUST agree: capping only at assembly
    # would make a 6-claim Deep output fail AFTER the parse gate, where the
    # repair loop no longer runs — so a recoverable shape error would become an
    # outright FAILED report. Capping here lets the existing repair path fix it.
    claims: list[Position] = Field(default_factory=list, max_length=5)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)
    counter_evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)
    confirmed_facts: list[PointItem] = Field(default_factory=list, max_length=12)
    assumptions: list[AssuranceGap] = Field(default_factory=list, max_length=12)
    missing_evidence: list[PointItem] = Field(default_factory=list, max_length=12)
    # One entry per claim, so the same cap applies.
    evidence_strength: list[EvidenceStrength] = Field(default_factory=list, max_length=5)
    judgement: str = Field(..., min_length=1, max_length=2000)
    recommendation: str = Field(..., min_length=1, max_length=2000)
    citations: list[EvidenceItem] = Field(default_factory=list, max_length=20)
    # Set ONLY when this pass moved the provisional judgement. The pipeline
    # cannot verify the wording of a correction, so the contract is: a non-empty
    # `supersedes` must be accompanied by a `judgement` that restates it. An
    # empty string means "no reversal".
    supersedes: str = Field(default="", max_length=1000)


class CaptainReportContent(BaseModel):
    """The referee's report — the exact JSON shape the LLM must return.

    All list fields default to empty and `dispute_points` is explicitly allowed
    to stay empty. There is no schema pressure to invent a conflict, which is
    what keeps "不强行制造冲突" a structural property rather than a polite
    request in the prompt.

    `uncertainty` is REQUIRED and non-nullable: a report that cannot state what
    it does not know is not a referee's report. The prompt gives it content
    (window size, missing facts), and validation rejects an empty one.
    """

    model_config = ConfigDict(extra="forbid")

    discussion_summary: str = Field(..., min_length=1, max_length=3000)
    # 1..5. The lower bound is 1 because a window with no identifiable position
    # is a "not enough material" outcome, handled BEFORE the LLM call
    # (context.py) or as FAILED — never as a fabricated empty report.
    positions: list[Position] = Field(..., min_length=1, max_length=5)
    agreement_points: list[PointItem] = Field(default_factory=list, max_length=8)
    dispute_points: list[PointItem] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)
    uncertainty: str = Field(..., min_length=1, max_length=2000)
    suggestion: str = Field(..., min_length=1, max_length=2000)
    # Outside information used by the referee (Phase 3-2). ALWAYS written by the
    # pipeline from real provider results — the model neither produces nor
    # invents these, and it cannot cite a source_id that was not supplied
    # (see `external_evidence.py`). Empty is the normal case: it means either the
    # policy decided no search was needed, or the search did not return in time.
    external_evidence: list[ExternalEvidence] = Field(default_factory=list)
    # --- Phase C.5: how the research attempt itself went -------------------
    # One of `RESEARCH_STATUSES`. Written by the pipeline, never by the model.
    # `not_required` is the default rather than a neutral empty string so that a
    # report_json persisted before Phase C.5 deserialises to the truthful value
    # for those rows (no research layer existed to require anything), and so
    # that the frontend has a single value to render as "no external research
    # happened" instead of two (`""` and `"not_required"`) that mean the same
    # thing.
    #
    # This is a STRING, not a bool: the difference between `partial` (some
    # queries answered, others timed out) and `failed` is the difference between
    # "the referee looked and found little" and "the referee could not look",
    # and collapsing those is exactly the P0 failure this phase exists to make
    # visible rather than silent.
    research_status: str = Field(default=RESEARCH_STATUSES[0], max_length=16)

    # --- two-stage assessment (Task 3) -------------------------------------
    # OPTIONAL, and the optionality is the compatibility contract: a report
    # written before the two-stage call chain existed (or produced by a
    # single-call provider) validates unchanged with both fields None, and the
    # flat fields above are still filled. That is what lets the two-stage
    # pipeline ship without a data migration or a flag day for existing rows.
    #
    # When they ARE present, the flat fields are DERIVED from them by
    # `service._assemble_staged_content` — `positions` from `deep.claims`,
    # `evidence` from `deep.evidence`, `suggestion` from `deep.recommendation`,
    # `dispute_points` from the claims when there is more than one. Deriving in
    # one place is what keeps the two views (flat = what the UI and the evidence
    # validator read, staged = what a reader inspecting the reasoning reads)
    # from ever disagreeing.
    quick: QuickAssessment | None = None
    deep: DeepAssessment | None = None

    @property
    def has_assessments(self) -> bool:
        """True when either stage produced output.

        Used by the read path to decide whether to offer the Quick result while
        Deep is still running, without re-implementing the None checks.
        """
        return self.quick is not None or self.deep is not None


class DeepReportContent(BaseModel):
    """The wire shape of ONE Deep call — what the model is asked to return.

    Deliberately NOT `CaptainReportContent`, and the difference is the point:
    the model is asked for the material it alone can produce (the judgement,
    the claims, the allocation of facts between confirmed / assumed / missing)
    and for the two things that describe the WHOLE window rather than the
    dispute. Everything else — `positions`, `evidence`, `dispute_points`,
    `suggestion`, `external_evidence` — is DERIVED by the pipeline from this
    output, so the model cannot return a `positions` list that disagrees with
    the `claims` it was just asked about.

    `DeepAssessment` is nested rather than flattened so the parse gate can point
    at one field when the shape is wrong, and so the stored report keeps the
    stage boundary visible.
    """

    model_config = ConfigDict(extra="forbid")

    discussion_summary: str = Field(..., min_length=1, max_length=3000)
    agreement_points: list[PointItem] = Field(default_factory=list, max_length=8)
    uncertainty: str = Field(..., min_length=1, max_length=2000)
    deep: DeepAssessment



class CaptainReport(CaptainReportContent):
    """The durable report: content plus the pipeline-owned scalars.

    Returned by the read API and persisted as `captain_report.report_json` with
    the scalars around it. `report_id` is the client's handle for a specific
    report (a meeting accumulates several over its lifetime).
    """

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    meeting_id: str
    status: str = "PENDING"
    anchor_segment_seq: int
    window_from_seq: int
    window_to_seq: int
    model_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CaptainReportPublic(BaseModel):
    """Read-only product projection.

    Identical substance to `CaptainReport` for now, kept as a SEPARATE type so
    the internal surface can grow engineering-only fields later without
    changing what the browser sees. AGENT-1 learned this the hard way: keeping
    one model for both surfaces is how `model_name` nearly became a product
    field (see `MeetingStatePublic`).
    """

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    meeting_id: str
    status: str = "PENDING"
    anchor_segment_seq: int
    window_from_seq: int = 0
    window_to_seq: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    # Content is absent while PENDING/ANALYZING/FAILED, present once COMPLETED.
    report: CaptainReportContent | None = None


__all__ = [
    "Position",
    "PointItem",
    "EvidenceItem",
    "ExternalEvidence",
    "AssuranceGap",
    "EvidenceStrength",
    "EvidenceStrengthLevel",
    "QuickAssessment",
    "DeepAssessment",
    "DeepReportContent",
    "CaptainReportContent",
    "CaptainReport",
    "CaptainReportPublic",
]
