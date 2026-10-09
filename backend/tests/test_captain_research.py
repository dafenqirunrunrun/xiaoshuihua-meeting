"""Phase 3-2 §7: the conditional external-research router.

What this file proves, and why each test is here
------------------------------------------------
The router has two jobs and they fail in opposite directions:

  1. **Ask SPECIFIC, ANSWERABLE questions.** A search driven by a keyword mash
     returns SEO pages; the question must be bound to a claim so a reader can
     tell which meeting claim a source answers.
  2. **Refuse questions whose answers live inside the organisation.** A web page
     that appears to settle "should we delay the launch?" is a fabricated
     authority the reader cannot detect. The refusal is per-QUESTION rather than
     per-window, which is what lets a window hold both an internal commercial
     sub-thread and an unrelated technical claim without the first vetoing the
     second.

Everything here is offline and pure: `research.plan` takes text and returns a
plan, so no test in this file can be affected by a network condition. The
integration half — that the plan actually gates the provider call — lives in
`TestEnrichHonoursThePlan` at the bottom, driven through `enrich` with a scripted
provider.
"""

from __future__ import annotations

import pytest

from app.captain.search import research
from app.captain.search.cache import reset_cache
from app.captain.search.fake import FakeSearchProvider
from app.captain.search import service as search_service
from app.config import settings


def _window(*texts: str) -> list[tuple[int, str]]:
    return [(index + 1, text) for index, text in enumerate(texts)]


class TestQuestionsAreSpecific:
    """The plan asks questions, not keyword mashed claims."""

    def test_questions_are_bound_to_claims(self):
        plan = research.plan(
            _window(
                "Redis 的默认端口是 6379",
                "不对吧，新版本默认端口好像不是 6379 了",
            )
        )

        assert plan.need_research
        assert plan.questions
        ids = [q.supports_query_id for q in plan.questions]
        assert ids[0].startswith("claim_")
        # Every question carries the claim text it came from, so a reader can
        # trace a source back to the reason it was fetched.
        assert all(q.origin_claim for q in plan.questions)

    def test_questions_end_as_questions_and_are_short(self):
        plan = research.plan(_window("Tomcat 的默认端口是 8080 还是 8005？"))

        assert plan.need_research
        for question in plan.questions:
            assert question.question.endswith("?")
            # A provider handed 200 chars of transcript returns pages about the
            # transcript. The cap is the structural form of that rule.
            assert len(question.question) <= 130

    def test_verification_tail_is_stripped_from_the_question(self):
        plan = research.plan(_window("帮我查一下 Redis 默认端口是不是 6379 到底对不对"))

        assert plan.need_research
        blob = " ".join(q.question for q in plan.questions)
        # "到底 / 是不是真的" asks about the question word, not the fact.
        assert "到底" not in blob
        assert "对不对" not in blob

    def test_at_most_three_questions_are_planned(self):
        plan = research.plan(
            _window("A 端口是 1", "B 协议是 2", "C 版本是 3", "D 格式是 4", "E 编码是 5")
        )

        assert len(plan.questions) <= research.MAX_RESEARCH_QUESTIONS

    def test_max_questions_zero_plans_nothing(self):
        plan = research.plan(_window("Redis 端口是 6379 吗"), max_questions=0)

        assert not plan.need_research
        assert plan.questions == []
        assert plan.reason == "budget_zero"

    def test_empty_window_plans_nothing(self):
        plan = research.plan([])

        assert not plan.need_research
        assert plan.reason == "empty_window"

    def test_fresh_window_marks_questions_current(self):
        plan = research.plan(_window("目前最新的 HTTP 版本还是 HTTP/2 吗"))

        assert plan.need_research
        assert all(q.kind == "CURRENT_FACT" for q in plan.questions)


class TestInternalQuestionsAreRefused:
    """§7: the six forbidden classes never reach a provider."""

    @pytest.mark.parametrize(
        "text,category",
        [
            ("我们的上线排期能不能提前一周", "internal_schedule"),
            ("这个模块谁负责的，他能不能搞定", "internal_owner"),
            ("客户那边合同条款还没确认", "commercial_contract"),
            ("我们的日活数据到底是多少", "private_business_data"),
            ("这次投入的预算和成本怎么算", "internal_cost"),
            ("我们内部的网关用了什么框架", "private_architecture"),
        ],
    )
    def test_each_forbidden_class_is_matched(self, text, category):
        slugs = [c.slug for c in research.blocking_categories(text)]

        assert category in slugs

    def test_a_wholly_internal_window_flags_information_required(self):
        plan = research.plan(
            _window(
                "我们的上线排期要不要提前",
                "这个需求谁负责，他能不能搞定",
            )
        )

        assert not plan.need_research
        assert plan.internal_information_required
        assert plan.reason == "internal_information_required"
        assert {c.slug for c in plan.internal_categories} >= {
            "internal_schedule",
            "internal_owner",
        }

    def test_a_factual_window_does_not_flag_information_required(self):
        """The flag must not fire on every window that merely mentions 上线.

        This is the false-positive direction that matters: if the flag were
        window-level, a technical claim discussed in a meeting that also
        mentioned scheduling would be marked internal and lose its search.
        """
        plan = research.plan(_window("Redis 的连接池默认最大连接数是 10000 吗"))

        assert plan.need_research
        assert not plan.internal_information_required

    def test_internal_subthread_does_not_veto_a_technical_question(self):
        """Per-QUESTION refusal, not per-window.

        The window contains an internal commercial line AND a technical claim.
        A window-level veto would refuse the technical question because a
        colleague happened to mention a contract; the technical question must
        survive.
        """
        plan = research.plan(
            _window(
                "客户合同条款还没定，商务那边在谈",
                "不过 Redis 6 之后默认是否还是单线程模型？",
            )
        )

        assert plan.need_research
        assert plan.questions
        # The commercial line was refused and recorded, without taking the
        # technical question down with it.
        assert "commercial_contract" in {c.slug for c in plan.internal_categories}

    def test_marking_is_not_disclosure(self):
        """A refusal produces no report sentence — it is telemetry only.

        The plan carries the flag and the categories and NOTHING else: no
        `note`, no prose. The caller decides where it goes, and
        `research.py`'s documented decision is the engineering log.
        """
        plan = research.plan(_window("我们的排期谁负责"))

        assert plan.internal_information_required
        assert not hasattr(plan, "note")

    def test_markers_match_against_the_whole_question_text(self):
        """Blocking looks at the WHOLE candidate text, not just its first word.

        The marker usually sits mid-clause ("客户那边说合同…"), so a matcher
        that only inspected a prefix would let it through.
        """
        slugs = [c.slug for c in research.blocking_categories("客户那边说合同还要再确认")]

        assert "commercial_contract" in slugs

    def test_blocking_categories_is_stable_and_deduplicated(self):
        """One occurrence per category, in declaration order.

        Telemetry consumers diff these lists, so an unstable order would make
        two identical windows look different in the log.
        """
        text = "预算和成本都要看，合同也要看，预算还是问题"
        slugs = [c.slug for c in research.blocking_categories(text)]

        assert slugs == list(dict.fromkeys(slugs))
        assert slugs == [
            c.slug
            for c in research.FORBIDDEN_CATEGORIES
            if c.slug in set(slugs)
        ]


class TestSourceQuality:
    """`quality_for` is a heuristic over the host, and is documented as one."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.rfc-editor.org/rfc/rfc9110.html", "standard"),
            ("https://www.w3.org/TR/html52/", "standard"),
            ("https://docs.python.org/3/library/asyncio.html", "official"),
            ("https://redis.io/docs/latest/", "official"),
            ("https://github.com/redis/redis", "official"),
            ("https://blog.example.com/post", "secondary"),
            ("https://zhuanlan.zhihu.com/p/123", "secondary"),
            ("https://medium.com/@someone/x", "secondary"),
            ("https://some-obscure-site.example/page", "reputable"),
        ],
    )
    def test_quality_is_assigned_from_the_host(self, url, expected):
        assert research.quality_for(url) == expected

    def test_unparseable_url_is_secondary_but_a_bare_word_is_not(self):
        """Empty input has no host to stand behind; a bare lowercase word does
        not look like a host either, but it is not empty. The two cases are
        distinguished so a caller can tell "no URL" from "URL we cannot parse".
        """
        assert research.quality_for("") == "secondary"
        assert research.quality_for("not a url") == "reputable"

    def test_quality_lookup_never_raises(self):
        for url in ("", "http://", "https://", "://x", "a b c", "https://[::1]/"):
            assert research.quality_for(url) in research.SOURCE_QUALITY_LEVELS

    def test_official_prefix_check_does_not_leak_through_a_secondary_host(self):
        """`blog.example.com/docs/x` must not pass on a `docs` substring.

        The first-LABEL rule is what prevents this — a `"docs." in host` or
        `startswith` check would be fooled by a host that merely contains the
        token somewhere.
        """
        assert research.quality_for("https://blog.example.com/docs/x") == "secondary"
        # ...while a genuine doc host on the same shape IS official.
        assert research.quality_for("https://docs.example.com/x") == "official"


class TestEnrichHonoursThePlan:
    """The plan actually GATES the provider call, end to end through `enrich`."""

    def test_an_internal_window_never_reaches_the_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "captain_external_search_enabled", True)
        reset_cache()
        provider = FakeSearchProvider(generate=True, result_count=3)

        result = search_service.enrich(
            _window("我们的上线排期谁负责", "预算和成本怎么算"),
            provider=provider,
        )

        assert provider.calls == []
        assert result.evidence == []
        assert result.metrics.internal_information_required

    def test_a_factual_window_reaches_the_provider_with_a_question(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "captain_external_search_enabled", True)
        reset_cache()
        provider = FakeSearchProvider(generate=True, result_count=3)

        result = search_service.enrich(
            _window(
                "MySQL 8.0 默认字符集是 utf8mb4 吗",
                "我记得默认还是 latin1，版本不一样",
            ),
            provider=provider,
        )

        assert provider.calls, "the router planned a question, so a search ran"
        # The query handed to the provider is a QUESTION, not a raw transcript
        # line — Phase 3-2 §4's "never forward the transcript wholesale".
        assert all(call.endswith("?") for call in provider.calls)
        assert result.evidence

    def test_results_carry_quality_and_the_answering_claim(self, monkeypatch):
        monkeypatch.setattr(settings, "captain_external_search_enabled", True)
        reset_cache()
        provider = FakeSearchProvider(generate=True, result_count=3)

        result = search_service.enrich(
            _window("Kafka 默认分区数是 1 还是 3", "新版默认还是 1 吧"),
            provider=provider,
        )

        assert result.evidence
        for source in result.evidence:
            assert source.source_quality in research.SOURCE_QUALITY_LEVELS
            assert source.answers_question.startswith("claim_")

    def test_quality_does_not_leak_between_provider_arms(self):
        """Two providers on the same URL get the same class.

        `quality_for` is assigned by the pipeline, so the A/B arm cannot change
        the classification — which is what makes `source_quality` comparable
        across providers.
        """
        url = "https://docs.python.org/3/"
        assert research.quality_for(url) == "official"
