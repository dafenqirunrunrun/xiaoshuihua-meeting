"""SearchPolicy — decide whether this discussion needs external facts.

PURE. No DB, no network, no LLM, no settings read inside the decision itself.
Input is the transcript window as plain text lines; output is a decision plus
the short query strings to send. This is the whole reason the module exists as
its own file: it is the piece most likely to be replaced (rules -> classifier ->
LLM router) and the piece that must stay trivially unit-testable.

Why NOT an LLM router (Phase 3-2 §1)
------------------------------------
The obvious design is "ask the model if a search is needed, then search, then
summarise". That is two LLM calls and a second failure surface for a feature
whose latency budget is 900ms. The adversarial example is decisive: with a
router in the loop, the model could decide to search on "周五上线还是下周上线?"
— an internal scheduling decision with no external answer — and burn the budget
retrieving nothing useful. Rules cannot make that mistake for a reason that can
be read off the code.

Four categories (Phase 3-2 §2)
------------------------------
  INTERNAL_DECISION      no search   "周五上线还是下周上线?"
  FACTUAL                search      "鸡和蛋哪个先出现?"
  CURRENT_FACT           search      "现在某产品是否支持某功能?"
  USER_REQUESTED_VERIFY  search      "水华队长，帮我们查一下谁说得对"

Precedence is deliberate and the order below IS the specification:

  1. USER_REQUESTED_VERIFY  — the user explicitly asked. This WINS OVER
     INTERNAL_DECISION, and that is the intended behaviour: "帮我查一下这个
     排期合不合理" contains a decision-ish topic but is an explicit request for
     outside information. Refusing it because the topic looked internal would be
     the policy second-guessing a direct instruction.
  2. INTERNAL_DECISION      — checked BEFORE factual markers, because internal
     topics routinely contain fact-shaped words: "我们的 QPS 是 1200 还是 1500"
     reads as a number dispute but only the meeting's own telemetry knows.
  3. CURRENT_FACT           — time-sensitive factual claims.
  4. FACTUAL                — verifiable factual claims.
  5. out-of-band            — marker sets too weak; seen markers + a question.

The claim-dispute requirement
-----------------------------
The single most important guard: a factual claim ALONE does not trigger a
search. `_looks_like_claim_dispute` requires at least two claim markers from
DIFFERENT segments, so a crowd of `认为`s in one utterance cannot impersonate a
disagreement. This is what keeps "我认为应该先把功能做完整" (an opinion, no
external answer) from spending the search budget. An opinion is not a fact
claim, and a monologue is not a dispute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.captain.search.schemas import FreshnessClass

PolicyType = Literal[
    "INTERNAL_DECISION",
    "FACTUAL",
    "CURRENT_FACT",
    "USER_REQUESTED_VERIFY",
    "NO_SEARCH",
]

# --- marker sets -----------------------------------------------------------
# All lowercase; matching normalises the input instead of the patterns, so a
# future marker cannot be added in the wrong case and silently never fire.

# The user explicitly asked for outside information.
_REQUEST_MARKERS: tuple[str, ...] = (
    "查一下", "查一查", "帮我查", "帮忙查", "搜一下", "搜一搜", "搜搜",
    "检索", "核实", "核对", "验证一下", "证实", "求证",
    "对不对", "是不是真的", "哪个对", "谁说得对", "谁说的对", "谁对",
)

# Verifiable factual claims (as opposed to preferences).
_CLAIM_MARKERS: tuple[str, ...] = (
    "是", "不是", "有", "没有", "支持", "不支持", "可以", "不可以",
    "能", "不能", "默认", "等于", "属于", "叫做", "叫做",
    "认为", "觉得", "说法", "事实", "实际上", "其实是", "正确", "错误",
)

# Time sensitivity. Kept SEPARATE from claims because they compose: a claim that
# is also current is CURRENT_FACT, which gets the short cache TTL (Phase 3-2 §9).
_CURRENT_MARKERS: tuple[str, ...] = (
    "现在", "目前", "如今", "当前", "最新", "最近", "今天", "本周", "这周",
    "本月", "今年", "刚刚", "实时", "截至", "截止", "到目前为止", "最新版本",
)

# Topics whose answers live INSIDE the organisation. Their presence means an
# external search cannot answer the question, however factual it sounds.
#
# SCOPED (Phase C.5 follow-up — the false-negative fix)
# -----------------------------------------------------
# Each marker here VETOES a search outright, so each must name a subject whose
# answer is a fact about THIS team. The audit found the previous list vetoing on
# bare generic words — "我们", "公司", "产品", "团队", "资源", "需求", "项目",
# "发布" — which appear constantly in FACTUAL discussion ("我们观察到 Redis 默认
# 端口是 6379 吗?", "这个产品文档写着…"). Those refusals are the expensive error:
# the user asked a checkable question and got no search at all, with no signal
# saying why. A marker now has to be specific enough that its presence really
# does mean "only the meeting knows".
#
# "发布" was dropped for the same reason: "发布说明说 X" is a citable external
# artifact, while "上线日期" is internal scheduling — the strong phrase 上线
# covers the latter without also killing the former.
_INTERNAL_MARKERS: tuple[str, ...] = (
    # Scheduling — the canonical case, and the one the adversarial test pins.
    "上线", "排期", "工期", "里程碑", "上线时间", "发布时间",
    # Staffing / ownership.
    "谁负责", "负责人", "分工", "人手", "换人", "团队能力",
    # Budget / cost.
    "预算", "报价", "报价单", "报销", "人力成本", "服务器成本",
    # Customer / contract.
    "甲方", "合同", "违约金", "采购", "续约", "签约",
    # Meeting-process state: the answer is a property of THIS conversation.
    # "会议是否已经达成共识?" has no external source by construction.
    "共识", "会议", "议程", "表决",
    # First-person org and private data / architecture.
    "咱们", "本公司", "内部数据", "我们的数据", "我们的架构", "我们的服务",
    "内网", "私有部署",
)

# Question shapes. Used only for the out-of-band path.
_QUESTION_MARKERS: tuple[str, ...] = ("?", "？", "吗", "呢", "是不是", "对不对", "还是")

# Digits matter for model numbers, versions, ports and dates.
_NUMBER_RE = re.compile(r"\d")

# Contradiction between two segments.
_CONTRAST_MARKERS: tuple[str, ...] = (
    "但是", "但", "不过", "然而", "其实不", "并不是", "并非", "恰恰相反", "相反",
)
_NEGATION_MARKERS: tuple[str, ...] = ("不是", "不对", "并非", "没有", "不支持", "不能")

# Time-sensitive query qualifier, REUSED from _CURRENT_MARKERS set below.
_FRESH_QUALIFIERS: tuple[str, ...] = ("现在", "最新", "当前", "目前")

# Sentence / clause splitters for short query extraction.
_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】\s]+")

MAX_QUERIES = 2


@dataclass(frozen=True)
class SearchDecision:
    """The policy's verdict, with enough provenance to explain it in a log."""

    policy_type: PolicyType
    should_search: bool
    freshness: FreshnessClass
    # 0..2 short queries. Empty when `should_search` is False.
    queries: list[str] = field(default_factory=list)
    # Short slug for telemetry (`SearchMetrics.search_policy_reason`).
    reason: str = ""


def _has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _matches(text: str, needles: tuple[str, ...]) -> list[str]:
    return [n for n in needles if n in text]


def _looks_like_claim_dispute(segments: list[str]) -> bool:
    """True when >=2 SEGMENTS each carry a substantive claim marker.

    Cross-segment on purpose: one person saying "我认为…我认为…" is a single
    opinion, not a disagreement, and must not trigger a search.

    SUBSTANTIVE, not any marker (Phase C.5 follow-up). A marker like "可以" or
    "是" occurs in almost every Chinese sentence, so counting any marker made a
    mundane observation exchange ("张三认为应该先做成本核算" / "李经理表示可以
    下周给数据") look like a factual dispute — which searched and pulled in
    external pages about the transcript. Only markers that actually assert a
    FACT (`_OPINION_ONLY` verbs like 认为/觉得/表示 are excluded here, though
    they still count as `claim_markers` for the out-of-band step) can establish
    a dispute. An opinion exchange is not a fact dispute.
    """

    def substantive(seg: str) -> bool:
        return any(
            marker in seg
            for marker in _CLAIM_MARKERS
            if marker not in _OPINION_ONLY
        )

    return sum(1 for seg in segments if substantive(seg)) >= 2


def _looks_like_contradiction(segments: list[str]) -> bool:
    """True when some segment contradicts an earlier one.

    Catches "A: X is 6379" / "B: 不是" where B alone has no claim marker of its
    own beyond the negation, plus explicit contrast connectors.
    """
    if any(_has(seg, _CONTRAST_MARKERS) for seg in segments):
        return True
    return any(_has(seg, _NEGATION_MARKERS) for seg in segments)


def _is_current(segments: list[str]) -> bool:
    return any(_has(seg, _CURRENT_MARKERS) for seg in segments)


def _segment_texts(segments: list[tuple[int, str]]) -> list[str]:
    return [text.lower() for _seq, text in segments]


def _clean_clause(text: str) -> str:
    """A clause reduced to something safe to hand a search API.

    Query construction never forwards a raw transcript line: it strips
    punctuation, collapses whitespace and hard-caps length. Phase 3-2 §4 is
    explicit that the whole transcript must never be sent as a query, and the
    cap is the structural form of that rule.
    """
    return " ".join(_PUNCT_RE.sub(" ", text).split())


def _extract_user_query(segments: list[tuple[int, str]]) -> str:
    """Pull out what the user asked us to look up ("查一下 X" -> "X").

    Prefers the text following the request marker; falls back to the whole
    (cleaned) clause. Never returns more than the pass-through cap.
    """
    for _seq, text in reversed(segments):
        lowered = text.lower()
        for marker in _REQUEST_MARKERS:
            idx = lowered.find(marker)
            if idx < 0:
                continue
            tail = text[idx + len(marker):]
            # "查一下 X 到底是不是真的" — drop the trailing verification tail so
            # the query is about X, not about the question word.
            for follow in ("到底", "究竟", "是不是真的", "对不对", "是否"):
                tail = tail.replace(follow, " ")
            cleaned = _clean_clause(tail)
            if cleaned:
                return cleaned[:120]
            # Marker was sentence-final ("帮我们查一下。") — the subject is in
            # the rest of the clause instead.
            head = _clean_clause(text[:idx])
            if head:
                return head[:120]
    return ""


def _build_claim_query(segments: list[tuple[int, str]], *, current: bool) -> str:
    """Two opposing factual claims -> one short "A vs B 事实" query."""
    claimed = [text for _seq, text in segments if _has(text.lower(), _CLAIM_MARKERS)]
    parts: list[str] = []
    for raw in reversed(claimed):
        clause = _clean_clause(raw)
        if clause:
            parts.append(clause[:60])
        if len(parts) == 2:
            break
    if not parts:
        return ""
    query = " vs ".join(reversed(parts))
    if current:
        # Keep the time sensitivity: it changes what the right answer IS.
        query = f"最新 {query}"
    return f"{query} 事实"[:160]


def _build_fallback_query(segments: list[tuple[int, str]], *, current: bool) -> str:
    """Fallback: the most recent substantive clause, capped short."""
    for _seq, text in reversed(segments):
        cleaned = _clean_clause(text)
        if len(cleaned) >= 4:
            return (f"最新 {cleaned}" if current else cleaned)[:120]
    return ""


def decide(
    segments: list[tuple[int, str]],
    *,
    wake_triggered: bool = False,
) -> SearchDecision:
    """Classify the window and produce 0..2 short queries.

    `segments` is `[(segment_seq, text), ...]` in the window's own ASC order.
    A tuple list (not ORM objects) keeps this importable and testable without a
    session — the same stance `captain/evidence.py` takes.
    """
    if not segments:
        return SearchDecision("NO_SEARCH", False, "STATIC_FACT", [], "empty_window")

    texts = _segment_texts(segments)
    joined = " ".join(texts)

    requested = _has(joined, _REQUEST_MARKERS) or wake_triggered
    internal = _has(joined, _INTERNAL_MARKERS)
    claim_markers = _matches(joined, _CLAIM_MARKERS)
    current = _is_current(texts)
    dispute = _looks_like_claim_dispute(texts) or _looks_like_contradiction(texts)
    has_number = bool(_NUMBER_RE.search(joined))
    question = _has(joined, _QUESTION_MARKERS)

    # --- 1. explicit user request wins over everything ----------------------
    if requested:
        query = _extract_user_query(segments)
        if not query:
            query = _build_fallback_query(segments, current=current)
        if query:
            return SearchDecision(
                "USER_REQUESTED_VERIFY",
                True,
                "CURRENT_FACT" if current else "TECH_FACT",
                [query],
                "explicit_request",
            )
        return SearchDecision("NO_SEARCH", False, "STATIC_FACT", [], "request_but_no_query")

    # --- 2. internal decision: no external source can answer it ------------
    # Unchanged in structure — the FIX is in `_INTERNAL_MARKERS`: it no longer
    # fires on bare generic words. "我们" / "产品" used to veto here and refused
    # genuine fact-checking ("我们观察到 Redis 默认端口是 6379 吗?"); the surviving
    # markers name an internal subject, not just an internal pronoun, so a window
    # mentioning "我们" alongside a checkable technical claim now reaches step
    # 3/4 instead of dying here.
    if internal:
        return SearchDecision(
            "INTERNAL_DECISION", False, "STATIC_FACT", [], "internal_topic"
        )

    # --- 3/4. factual claims ------------------------------------------------
    # Guard rails before spending the budget:
    #   - the disagreement must be real (cross-segment markers or a contrast)
    #   - it must be about a checkable FACT, not a preference. A preference
    #     reads as a claim marker ("我觉得") and would otherwise pass, so a
    #     bare "I think" is explicitly disqualified unless the statement also
    #     carries something checkable (a number or an explicit claim marker
    #     beyond opinion verbs).
    if dispute and _is_checkable(joined, claim_markers, has_number):
        if current:
            query = _build_claim_query(segments, current=True)
            if query:
                return SearchDecision(
                    "CURRENT_FACT", True, "CURRENT_FACT", [query], "current_claim_dispute"
                )
        query = _build_claim_query(segments, current=False)
        if not query:
            query = _build_fallback_query(segments, current=False)
        if query:
            return SearchDecision(
                "FACTUAL", True, "TECH_FACT", [query], "factual_claim_dispute"
            )

    # --- 5. out-of-band: strong markers + a question + a checkable element --
    if claim_markers and question and _is_checkable(joined, claim_markers, has_number):
        query = _build_claim_query(segments, current=current)
        if not query:
            query = _build_fallback_query(segments, current=current)
        if query:
            return SearchDecision(
                "CURRENT_FACT" if current else "FACTUAL",
                True,
                "CURRENT_FACT" if current else "TECH_FACT",
                [query],
                "factual_question",
            )

    return SearchDecision(
        "NO_SEARCH",
        False,
        "CURRENT_FACT" if current else "STATIC_FACT",
        [],
        "no_verifiable_claim",
    )


# Opinion / attribution verbs. Used ONLY to decide whether a claim marker is
# substantive — they assert what someone SAID, not a fact the world can settle.
# `表示`/`说` are included because "李经理表示可以…" attributes a stance rather
# than asserting a checkable fact; the copula `可以` inside it must not be read
# as a factual claim.
_OPINION_ONLY: frozenset[str] = frozenset({"认为", "觉得", "表示", "说"})


def _is_checkable(
    joined: str, claim_markers: list[str], has_number: bool
) -> bool:
    """Is there something here that an external source could actually settle?

    A number is self-evidently checkable (ports, versions, dates, QPS). Absent a
    number, at least one claim marker must be more than a bare opinion verb —
    "我认为应该先做功能" is a preference and no search can resolve it.
    """
    if has_number:
        return True
    substantive = [m for m in claim_markers if m not in _OPINION_ONLY]
    return bool(substantive)


__all__ = [
    "SearchDecision",
    "PolicyType",
    "MAX_QUERIES",
    "decide",
]
