"""ResearchRouter — turn a search decision into SPECIFIC, ANSWERABLE questions.

PURE. No DB, no network, no LLM, no settings read inside the decision itself.
Input is the same `[(segment_seq, text)]` window `policy.decide` takes; output is
a plan of 2-3 concrete research questions plus, when research was considered and
refused, the reason it was refused.

Why this is a separate module from `policy.py`
----------------------------------------------
`policy.decide` answers ONE question: *is this a factual dispute at all?* Its
output is a coarse verdict plus a query string built by joining two claims.
That is enough to know search is warranted and not enough to search WELL — "最新
X vs Y 事实" is a keyword mash, not a question, and a provider given a keyword
mash returns SEO pages.

This module answers the next question: *what exactly would we ask, and is asking
it even legitimate?* Two consequences follow, and both are the point:

  1. Questions are formed as QUESTIONS, each bound to a claim in the window, so
     the model that later reads `<external_evidence>` can tell which meeting
     claim a source is answering. A source with no question behind it is
     unfalsifiable decoration.
  2. A question that can only be answered from inside the organisation is
     REFUSED here, by name, rather than searched for and left to the model to
     quietly ignore. See `FORBIDDEN_CATEGORIES`.

The forbidden set is the load-bearing part
-----------------------------------------
Phase 3-2 §7 lists what the internet must never be used to settle: 内部排期,
内部负责人能力, 商务合同, 私有业务数据, 内部成本, 未公开架构事实. The spec's
requirement is not "prefer not to" — it is that these be *marked*, so the report
can say "this needs internal information" instead of pretending a web page
settled it.

Matching is done per QUESTION, not per window, and that distinction matters: a
window can legitimately contain both an internal commercial sub-thread and an
unrelated technical claim, and a window-level veto would refuse the technical
question because a colleague happened to mention a contract. A question that
survives is one whose own text carries no forbidden marker.

False positives are the intentional failure direction: a bare marker like "成本"
inside a genuinely technical question costs one uncited search, while a search
that answers "我们应该推迟上线吗?" with a web page is a fabricated authority the
reader cannot detect. When the two errors are asymmetric, lean toward refusing.

Marking is not disclosure
-------------------------
A refusal sets `ResearchPlan.internal_information_required` and records the
matched categories for telemetry. It deliberately does NOT produce a sentence
for the report's `uncertainty`: the policy declining to search is the CORRECT
outcome for most meeting topics, and a line on every such report would be noise
that trains readers to skip `uncertainty` entirely. The flag is reported where
it is actionable — the engineering log — and a report's `uncertainty` is
reserved for gaps the reader must weigh (see `search/service.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# How many questions may be planned for one analysis. Phase 3-2 §4 caps the
# provider calls; this caps the PLAN, so a plan can never ask for more budget
# than the caller is willing to spend.
MAX_RESEARCH_QUESTIONS = 3

# What kind of outside answer the question expects. Drives the cache TTL
# (`FreshnessClass`) and tells the reader what sort of authority would settle it.
ResearchKind = Literal["TECH_FACT", "CURRENT_FACT", "DEFINITION"]

# The provider-quality vocabulary (Phase 3-2 §6). Deliberately ordered by the
# authority a reader should grant the source, and deliberately NOT a score:
# "official" means the vendor or standards body itself said it, which is a
# different KIND of claim from three blogs agreeing.
SourceQuality = Literal["official", "standard", "reputable", "secondary"]

SOURCE_QUALITY_LEVELS: tuple[str, ...] = (
    "official",
    "standard",
    "reputable",
    "secondary",
)


@dataclass(frozen=True)
class ForbiddenCategory:
    """One topic class whose answer lives inside the organisation.

    `markers` are matched as substrings against a LOWERCASED question. Kept
    short and specific where possible: a sharp marker ("我们的架构") refuses
    almost nothing it should not, while a bare one ("成本") refuses too much.
    Where the trade was made, the comment says so.
    """

    slug: str
    label: str
    markers: tuple[str, ...]


# Phase 3-2 §7, one entry per forbidden class. Order is presentation order only;
# membership is a set.
FORBIDDEN_CATEGORIES: tuple[ForbiddenCategory, ...] = (
    ForbiddenCategory(
        "internal_schedule",
        "公司内部排期",
        (
            "排期", "工期", "上线时间", "发布时间", "什么时候上线", "几号上线",
            "里程碑", "交付时间", "推迟上线", "提前上线", "上线日期", "deadline",
        ),
    ),
    ForbiddenCategory(
        "internal_owner",
        "内部负责人能力 / 分工",
        (
            "谁负责", "负责人", "谁来负责", "谁做", "谁来做", "分工",
            "人手", "能不能搞定", "谁来顶", "换人", "他的能力", "团队能力",
        ),
    ),
    ForbiddenCategory(
        "commercial_contract",
        "商务合同 / 客户关系",
        (
            "合同", "报价", "商务", "甲方", "违约金", "采购", "订单",
            "客户要求", "客户能接受", "续约", "签约", "条款",
        ),
    ),
    ForbiddenCategory(
        "private_business_data",
        "私有业务数据",
        (
            "我们的数据", "我们的日活", "我们的月活", "我们的留存",
            "我们的转化", "我们的 gmv", "我们的营收", "内部数据",
            "我们的用户量", "我们的流量",
        ),
    ),
    ForbiddenCategory(
        "internal_cost",
        "内部成本 / 预算",
        (
            "预算", "成本", "费用", "支出", "人力成本", "服务器成本",
            "报销", "花了多少钱", "报价单",
        ),
    ),
    ForbiddenCategory(
        "private_architecture",
        "未公开架构事实",
        (
            "我们的架构", "我们的服务", "我们内部", "内部接口", "内网",
            "私有部署", "自研", "我们的网关", "我们的数据库", "我们用了什么",
        ),
    ),
)


@dataclass(frozen=True)
class ResearchQuestion:
    """One specific, answerable question bound to a claim in the window.

    `supports_query_id` is the LOCAL handle of the meeting claim the question
    came from (`claim_1`, `claim_2`, …). It exists so a later reader can see
    that a source answers a NAMED claim rather than referring to the
    discussion in general — the same discipline `ExternalEvidence.source_id`
    applies on the other side of the pipeline.
    """

    question: str
    kind: ResearchKind
    supports_query_id: str
    # What sort of source would settle it. Guidance for the reader; the actual
    # per-result quality is assigned from the URL by `quality_for`.
    expected_quality: SourceQuality
    # Which source claim text this question was derived from. Debug/telemetry.
    origin_claim: str = ""


@dataclass(frozen=True)
class ResearchPlan:
    """The router's verdict. Always present, even when nothing is planned."""

    need_research: bool
    questions: list[ResearchQuestion] = field(default_factory=list)
    # Categories that REFUSED a question that was otherwise warranted. Non-empty
    # only when research was wanted and could not legitimately proceed.
    internal_categories: list[ForbiddenCategory] = field(default_factory=list)
    # Short stable slug for telemetry. Always set.
    reason: str = ""

    @property
    def internal_information_required(self) -> bool:
        """True when the ONLY thing standing between us and a search was that
        the question is internal.

        This is the flag Phase 3-2 §7 asks to be marked, and it is deliberately
        narrower than "the window mentioned 上线": it fires only when a real
        candidate question was refused, so it answers "would outside research
        have helped if it had been allowed?" — which is the actionable question.
        """
        return bool(self.internal_categories) and not self.questions


# --- question shaping -------------------------------------------------------

# Sentence / clause splitters, shared in spirit with policy.py's cleaner.
_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】\s]+")

# Question tails that add nothing to a search query and actively hurt it.
_QUESTION_NOISE: tuple[str, ...] = (
    "到底", "究竟", "是不是真的", "对不对", "是不是", "是否", "吗", "呢",
)

# Time-sensitivity qualifier prepended for CURRENT_FACT questions, so the
# provider's own freshness bias is aligned with ours.
_FRESH_PREFIX = "最新 "


def _normalise(text: str) -> str:
    """Lowercased, punctuation-collapsed — the form every matcher sees."""
    return " ".join(_PUNCT_RE.sub(" ", text).split())


def blocking_categories(text: str) -> list[ForbiddenCategory]:
    """Every forbidden category whose markers appear in `text`.

    Substring matching against the lowercased form. Returns categories in the
    declaration order of `FORBIDDEN_CATEGORIES` so telemetry is stable.
    """
    normalised = _normalise(text).lower()
    return [
        category
        for category in FORBIDDEN_CATEGORIES
        if any(marker in normalised for marker in category.markers)
    ]


def _question_from_claim(claim: str) -> str:
    """A clause reduced to a question a provider can answer.

    The transcript is never forwarded wholesale (Phase 3-2 §4): punctuation is
    collapsed, the verification tail is dropped ("到底是不是真的" asks about the
    question word, not the fact), and length is hard-capped. What survives is
    the bare topic plus a question mark, which is what turns a keyword mash into
    something a search API can match.
    """
    cleaned = _normalise(claim)
    for noise in _QUESTION_NOISE:
        cleaned = cleaned.replace(noise, " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    # Keep it a question, and keep it short. A provider given 200 characters of
    # transcript returns pages about the transcript.
    if not cleaned.endswith("?"):
        cleaned = f"{cleaned}?"
    return cleaned[:120]


# --- quality assignment -----------------------------------------------------

# First labels that mean "this host is the project documenting itself".
# Checked as the first label only, since `docs.foo.com` is a doc host while
# `foo.docs.com` is not.
_OFFICIAL_HOST_LABELS: frozenset[str] = frozenset(
    {"docs", "developer", "developers", "api", "support", "help"}
)

_STANDARD_DOMAINS: tuple[str, ...] = (
    "w3.org", "ietf.org", "rfc-editor.org", "iso.org", "iec.ch",
    "ieee.org", "unicode.org", "iana.org", "oasis-open.org", "khronos.org",
)

_REPUTABLE_DOMAINS: tuple[str, ...] = (
    "wikipedia.org", "github.com", "stackoverflow.com", "mozilla.org",
    "apache.org", "python.org", "nodejs.org", "postgresql.org", "mysql.com",
    "redis.io", "kernel.org", "nginx.org", "docker.com", "kubernetes.io",
    "spring.io", "docs.rs", "npmjs.com", "pypi.org",
)

# TLDs/hosts that are structured as user-generated content, not authority.
_SECONDARY_HINTS: tuple[str, ...] = (
    "blog.", "medium.com", "csdn.net", "cnblogs.com", "juejin.cn",
    "zhihu.com", "jianshu.com", "segmentfault.com", "reddit.com",
    "quora.com", "baidu.com", "baijiahao.baidu.com", "sohu.com",
    "toutiao.com", "weixin.qq.com", "mp.weixin.qq.com", "bilibili.com",
)


# Host labels that mean "this is a community platform" — the `source_type`
# counterpart of the `secondary` quality tier. Shares the spirit of
# `_SECONDARY_HINTS` but the two lists are NOT the same: `zhihu.com` is a
# community platform AND a secondary-quality source, while `docs.foo.com` is
# documentation AND official-quality. Two axes, two vocabularies.
_COMMUNITY_HOSTS: tuple[str, ...] = (
    "stackoverflow.com", "stackexchange.com", "reddit.com", "quora.com",
    "zhihu.com", "csdn.net", "cnblogs.com", "juejin.cn", "segmentfault.com",
    "jianshu.com", "discourse", "forum", "bbs.", "club.",
    # Code-hosting and user-edited encyclopaedias: the content is contributed,
    # not issued by an authority, which is the community axis even though a
    # repository there can be the most authoritative thing about a project.
    "github.com", "gitlab.com", "wikipedia.org",
)

# Doc-site path fragments. `developer.mozilla.org/en-US/docs/Web/API` is
# documentation whatever its host, and a vendor's API reference served under
# `/reference/` is too.
_DOC_PATH_HINTS: tuple[str, ...] = ("/docs/", "/doc/", "/documentation/", "/reference/", "/api/", "/manual/")

# Journal / pre-print hosts and arXiv-style URL shapes.
_PAPER_HOSTS: tuple[str, ...] = (
    "arxiv.org", "doi.org", "dl.acm.org", "ieeexplore.ieee.org",
    "springer.com", "link.springer.com", "sciencedirect.com", "nature.com",
    "science.org", "acm.org", "openreview.net", "biorxiv.org", "medrxiv.org",
)

# News / editorial hosts. A news OUTLET reporting on a vendor is `media` — a
# different kind of statement from the vendor's own page.
_MEDIA_HOSTS: tuple[str, ...] = (
    "reuters.com", "bloomberg.com", "cnbc.com", "ft.com", "wsj.com",
    "nytimes.com", "theverge.com", "techcrunch.com", "arstechnica.com",
    "36kr.com", "infoq.com", "oschina.net", "ithome.com", "thepaper.cn",
    "caixin.com", "yicai.com", "jiemian.com",
)


def _host_of(url: str) -> str:
    """Lowercased host from a URL, or "" when it cannot be parsed.

    Shared by `quality_for` and `source_type_for` so the two fields can never
    disagree about what host a result is on. Dependency-free and forgiving for
    the same reason `domain_of` is: one malformed URL must not discard a whole
    response, and it must not raise inside the enrichment path either.
    """
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    without_scheme = raw.split("://", 1)[1] if "://" in raw else raw
    host = without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return host.split("@", 1)[-1].split(":", 1)[0]


def quality_for(url: str, *, title: str = "") -> SourceQuality:
    """Best-effort authority class for one result. NEVER raises.

    Phase 3-2 §6 asks for a `source_quality` field and the honest answer is that
    this system cannot verify a web page — so the field is assigned from the
    only signal available (the host) and documented as a HEURISTIC, not a
    judgement. It is deliberately a coarse four-value class and not a score, for
    the same reason `EvidenceStrength.level` is not a percentage: a fine-grained
    number would imply a measurement nobody performed.

    Rules, in order:

      1. A standards body's own domain is `standard`.
      2. An official vendor/doc host (`docs.*`, `developer.*`, or a known
         project domain) is `official`.
      3. A known user-generated platform is `secondary`.
      4. Anything else that parsed a host is `reputable` — the default, because
         an unknown domain is not evidence of poor quality, and downgrading
         every unfamiliar site to `secondary` would make the field useless.

    An empty host yields `secondary`: an unparseable URL is not a source we can
    stand behind.
    """
    if not url and not title:
        return "secondary"
    host = _host_of(url)
    if not host:
        return "secondary"

    def _suffix_match(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if any(_suffix_match(d) for d in _STANDARD_DOMAINS):
        return "standard"
    if any(_suffix_match(d) for d in _REPUTABLE_DOMAINS):
        return "official"
    if any(hint in host for hint in _SECONDARY_HINTS):
        return "secondary"
    # `docs.foo.com` / `developer.foo.com` is a vendor documenting itself,
    # whoever the vendor is — but only when the prefix is the FIRST label, which
    # is what those names mean in practice. Checked AFTER the secondary hints so
    # `blog.docs.example` does not sneak past on a prefix match.
    if host.split(".", 1)[0] in _OFFICIAL_HOST_LABELS:
        return "official"
    return "reputable"


def source_type_for(url: str, *, title: str = "") -> str:
    """Best-effort KIND-of-source class for one result, or "" when unknown.

    The second axis of `source_quality`, and deliberately not derived from it.
    `source_type` answers "what is this thing?" from observable URL structure —
    a standards body's own page, a vendor's `/docs/` page, a journal host, a
    news outlet, a Q&A platform — while `quality_for` answers "how much does
    THIS system trust it?". A vendor's release blog and its API reference are
    both `official` by type and can differ by trust; a Reuters story about a
    vendor is `media` by type and still `reputable` by trust.

    Returns "" rather than "other" for an unusable URL: "" means "we could not
    classify this", "other" means "we looked and it fits nothing" — collapsing
    them would make the field claim a classification that never happened. The
    declared vocabulary is the `SourceType` Literal in `captain/schemas.py`, and
    a test pins that every value returned here is a member of it.
    """
    host = _host_of(url)
    if not host:
        return ""

    def _suffix_match(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    lowered_url = (url or "").lower()
    if any(_suffix_match(d) for d in _PAPER_HOSTS):
        return "paper"
    if any(host == d or host.endswith("." + d) for d in _STANDARD_DOMAINS):
        return "standard"
    if any(_suffix_match(d) for d in _MEDIA_HOSTS):
        return "media"
    if any(_suffix_match(d) for d in _COMMUNITY_HOSTS):
        return "community"
    # Documentation by PATH before documentation by HOST: `/docs/` on an
    # otherwise unknown domain is still a doc page, and the path check must not
    # be reachable only for hosts we happen to have listed.
    if any(hint in lowered_url for hint in _DOC_PATH_HINTS):
        return "documentation"
    if host.split(".", 1)[0] in _OFFICIAL_HOST_LABELS:
        return "documentation"
    if any(_suffix_match(d) for d in _REPUTABLE_DOMAINS):
        return "official"
    # A host we parsed but cannot place. Honest answer, not a guess.
    return "other"


# --- relation assignment ----------------------------------------------------

# Markers that a snippet NEGATES the thing it is about. Kept short and
# unambiguous on purpose: 「不」 alone appears inside 「不过」「不少」, so only
# full negating phrases count. The cost of a miss is a `contextual` label, which
# is the honest default anyway.
_CONTRADICT_MARKERS: tuple[str, ...] = (
    "不是", "并非", "不对", "不正确", "错误", "不再", "已废弃", "已弃用",
    "已过时", "已修复", "已经修复", "恰恰相反", "与之相反", "no longer",
    "deprecated", "incorrect", "not true", "is false", "已下线",
)

# Markers that a snippet AFFIRMS the thing it is about. Also deliberately
# narrow: 「官方」 alone is an attribution, not an assertion about the claim.
_SUPPORT_MARKERS: tuple[str, ...] = (
    "确认", "已确认", "确实", "符合", "一致", "支持", "证实", "已实现",
    "confirmed", "supports", "is correct", "as expected",
)

# Minimum character-bigram overlap between the claim and the snippet before a
# support/contradict label is even considered. Below this the snippet is merely
# on the same topic.
_RELATION_OVERLAP = 0.5

# A claim shorter than this cannot produce a meaningful bigram set.
_RELATION_MIN_BIGRAMS = 3


def _bigrams(text: str) -> set[str]:
    """Character bigrams of the normalised form — a tokenizer-free stand-in.

    Chinese has no whitespace tokenisation available here (no jieba in the
    dependency set, and adding one for this field alone is not worth it), so
    overlap is measured on character bigrams, which is a standard cheap
    approximation of term overlap for CJK and degrades gracefully on English.
    """
    normalised = _normalise(text).lower()
    return {normalised[i : i + 2] for i in range(len(normalised) - 1)}


def relation_for(snippet: str, claim: str) -> str:
    """How a snippet bears on the claim it was fetched for. NEVER raises.

    One of `support` / `contradict` / `contextual`. `contextual` is the answer
    for almost everything, and that is the design rather than a shortcoming: a
    search provider returns a passage that MENTIONS the topic, and a passage
    that mentions a topic does not thereby take a position on it. Forcing a
    stance onto every snippet would manufacture agreement or conflict that no
    source asserted — the same failure the whole role-separation phase exists to
    prevent, one layer down.

    So a stance is claimed ONLY when the snippet both (a) substantially overlaps
    the claim's own wording and (b) carries an explicit affirming or negating
    marker. Anything else is `contextual`. A wrong `support` is much worse than
    a missing one: the reader sees the referee appearing to have found
    corroboration, and cannot tell it was inferred from a substring.

    This is a HEURISTIC over the provider's snippet, and it is documented as one
    — it is a reading aid on a source the reader can open, not a verdict.
    """
    if not snippet or not claim:
        return "contextual"
    claim_grams = _bigrams(claim)
    if len(claim_grams) < _RELATION_MIN_BIGRAMS:
        return "contextual"
    snippet_grams = _bigrams(snippet)
    if not snippet_grams:
        return "contextual"
    overlap = len(claim_grams & snippet_grams) / len(claim_grams)
    if overlap < _RELATION_OVERLAP:
        return "contextual"
    lowered = snippet.lower()
    if any(marker in lowered for marker in _CONTRADICT_MARKERS):
        return "contradict"
    if any(marker in lowered for marker in _SUPPORT_MARKERS):
        return "support"
    return "contextual"


# --- planning ---------------------------------------------------------------

def plan(
    segments: list[tuple[int, str]],
    *,
    max_questions: int = MAX_RESEARCH_QUESTIONS,
    claim_markers: tuple[str, ...] | None = None,
) -> ResearchPlan:
    """Build 2-3 specific questions, or refuse with a reason.

    `segments` is `[(segment_seq, text)]` in window order — the same value
    `policy.decide` consumed. This function is called only when the policy
    already decided a search is warranted, so it does not re-litigate "is this a
    factual dispute"; it decides WHAT to ask and WHETHER asking is legitimate.

    Candidate questions are derived from the segments' own claim-bearing
    clauses, most recent first, de-duplicated textually, and each bound to a
    `claim_N` handle in the order the claims appear. Refusal is per question:
    the plan keeps every question whose own text is free of forbidden markers,
    and reports the categories that refused the rest.
    """
    if not segments:
        return ResearchPlan(False, [], [], "empty_window")

    limit = max(0, max_questions)
    if limit == 0:
        return ResearchPlan(False, [], [], "budget_zero")

    # Freshness is a property of the WINDOW (the policy decided it too), so
    # every question from this window shares it.
    fresh = any(
        marker in text
        for _seq, text in segments
        for marker in ("现在", "目前", "当前", "最新", "最近", "这周", "本周")
    )
    kind: ResearchKind = "CURRENT_FACT" if fresh else "TECH_FACT"
    expected: SourceQuality = "official"

    candidates: list[tuple[str, str]] = []  # (origin_claim, question)
    seen: set[str] = set()
    for _seq, text in reversed(segments):
        raw = text.strip()
        if len(raw) < 4:
            continue
        for clause in re.split(r"[，。、；：！？,.;:!?]", raw):
            clause = clause.strip()
            if len(clause) < 4:
                continue
            question = _question_from_claim(clause)
            if not question or question in seen:
                continue
            seen.add(question)
            candidates.append((clause, question))
            break  # one question per segment: a segment is one utterance

    if not candidates:
        return ResearchPlan(False, [], [], "no_question")

    refused: list[ForbiddenCategory] = []
    questions: list[ResearchQuestion] = []
    for index, (origin, question) in enumerate(candidates, start=1):
        blocked = blocking_categories(question) or blocking_categories(origin)
        if blocked:
            for category in blocked:
                if category not in refused:
                    refused.append(category)
            continue
        if kind == "CURRENT_FACT" and not question.startswith(_FRESH_PREFIX):
            question = f"{_FRESH_PREFIX}{question}"
        questions.append(
            ResearchQuestion(
                question=question,
                kind=kind,
                supports_query_id=f"claim_{index}",
                expected_quality=expected,
                origin_claim=origin,
            )
        )
        if len(questions) == limit:
            break

    if not questions:
        # Research was warranted and every candidate was refused for being
        # internal. THIS is the case the spec asks to be marked.
        return ResearchPlan(
            False, [], refused, "internal_information_required"
        )

    return ResearchPlan(True, questions, refused, "planned")


__all__ = [
    "FORBIDDEN_CATEGORIES",
    "MAX_RESEARCH_QUESTIONS",
    "ForbiddenCategory",
    "ResearchKind",
    "ResearchPlan",
    "ResearchQuestion",
    "SOURCE_QUALITY_LEVELS",
    "SourceQuality",
    "blocking_categories",
    "plan",
    "quality_for",
    "relation_for",
    "source_type_for",
]
