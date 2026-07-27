"""Execution: router, order store, crash recovery, reconciliation.

These test the failure modes, not the happy path. The happy path was never what loses
money — a timeout that gets retried is, a crash between intent and response is, and a
position book that quietly disagrees with the venue is.
"""

from __future__ import annotations

import pytest

from pmx.audit.ledger import EntryKind
from pmx.core.models import OrderState, Venue
from pmx.core.money import Usd
from pmx.execution.reconciler import reconcile, reconcile_cash
from pmx.execution.recovery import recover_orders
from pmx.execution.router import ExecutionRouter, OrderOutcomeUnknown
from pmx.execution.store import DuplicateOrder, OrderStore
from pmx.risk.engine import Decision, DecisionOutcome, RiskEngine
from pmx.risk.limits import Limit, Rejection
from pmx.venues.fees import FeeModel
from tests.conftest import NOW, make_fee_model_config
from tests.fakes import FakeVenue


@pytest.fixture
def venue() -> FakeVenue:
    return FakeVenue(Venue.KALSHI)


@pytest.fixture
def store(tmp_path) -> OrderStore:
    with OrderStore(tmp_path / "orders.db") as opened:
        yield opened


@pytest.fixture
def router(venue, store, ledger, tmp_path) -> ExecutionRouter:
    return ExecutionRouter(
        {str(Venue.KALSHI): venue},
        store,
        ledger,
        kill_file=str(tmp_path / "KILL_ABSENT"),
    )


@pytest.fixture
def approved(config, signal, market, snapshot) -> Decision:
    engine = RiskEngine(
        config,
        fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
    )
    decision = engine.evaluate(signal, market, snapshot, now=NOW)
    assert decision.outcome is DecisionOutcome.APPROVED
    return decision


class TestHappyPathAndAudit:
    def test_order_reaches_the_venue_and_is_recorded(self, router, venue, store, approved):
        result = router.submit(approved)
        assert len(venue.place_calls) == 1
        assert result.record.state is OrderState.SUBMITTED
        assert store.get(approved.order.idempotency_key).venue_order_id == result.venue_order_id

    def test_intent_is_written_before_the_network_call(
        self, venue, store, ledger, tmp_path, approved
    ):
        """If the process dies after this write, recovery has something to resolve. If
        the write happened after the call, a crash would leave no trace of an order that
        might exist."""
        seen: list[str] = []

        original = venue.place_order

        def spy(order):
            seen.append(store.get(order.idempotency_key).state)
            return original(order)

        venue.place_order = spy  # type: ignore[method-assign]
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        router.submit(approved)
        assert seen == [OrderState.PENDING_SUBMIT]

    def test_submission_is_audited(self, router, ledger, approved):
        router.submit(approved)
        kinds = [entry.kind for entry in ledger.entries()]
        assert str(EntryKind.ORDER_SUBMITTED) in kinds
        assert str(EntryKind.ORDER_ACKED) in kinds


class TestKillSwitch:
    def test_kill_file_blocks_submission(self, venue, store, ledger, tmp_path, approved):
        kill = tmp_path / "KILL"
        kill.write_text("")
        router = ExecutionRouter({str(Venue.KALSHI): venue}, store, ledger, kill_file=str(kill))
        from pmx.risk.circuit import KillSwitchEngaged

        with pytest.raises(KillSwitchEngaged):
            router.submit(approved)
        assert venue.place_calls == [], "no order may reach the venue with KILL present"

    def test_cancel_all_works_while_halted(self, venue, store, ledger, tmp_path, approved):
        """cancel_all runs *because* we are halting. Refusing to cancel would leave live
        orders on the book, which is what the kill switch exists to prevent."""
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        router.submit(approved)
        (tmp_path / "KILL").write_text("")
        cancelled = router.cancel_all()
        assert len(cancelled) == 1
        assert cancelled[0].state is OrderState.CANCELED


class TestAmbiguousTimeouts:
    def test_timeout_after_acceptance_is_unknown_not_failed(self, router, venue, store, approved):
        """The order exists at the venue. Marking it failed would be a lie that leads
        directly to a duplicate position."""
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)
        assert store.get(approved.order.idempotency_key).state is OrderState.UNKNOWN

    def test_timeout_before_acceptance_is_also_unknown(self, router, venue, store, approved):
        """From inside the process the two timeouts are indistinguishable, so they must
        be treated identically — as unknown, never as a safe retry."""
        venue.timeout_before_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)
        assert store.get(approved.order.idempotency_key).state is OrderState.UNKNOWN

    def test_unknown_order_is_audited_with_its_resolution_path(
        self, router, venue, ledger, approved
    ):
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)
        unknown = ledger.entries(kind=EntryKind.ORDER_UNKNOWN)
        assert len(unknown) == 1
        assert "query venue state" in str(unknown[0].payload["resolution"])

    def test_explicit_rejection_is_terminal_not_unknown(self, router, venue, store, approved):
        """A venue that answers 'no' has answered. That is not ambiguity."""
        venue.reject_with = "market closed"
        result = router.submit(approved)
        assert result.record.state is OrderState.REJECTED
        assert store.get(approved.order.idempotency_key).is_terminal


class TestDuplicateSubmission:
    def test_resubmitting_the_same_order_is_refused_by_the_store(self, router, venue, approved):
        """The retry-after-timeout path in its natural habitat. One intent, one order."""
        router.submit(approved)
        with pytest.raises(DuplicateOrder, match="never resubmit blind"):
            router.submit(approved)
        assert len(venue.place_calls) == 1

    def test_resubmission_after_an_unknown_outcome_is_also_refused(self, router, venue, approved):
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)
        venue.timeout_after_accepting = False
        with pytest.raises(DuplicateOrder):
            router.submit(approved)
        assert len(venue.place_calls) == 1, "the venue already has this order"

    def test_a_terminal_order_cannot_be_quietly_reopened(self, store, approved, router, venue):
        venue.reject_with = "nope"
        router.submit(approved)
        with pytest.raises(ValueError, match="terminal"):
            store.mark(approved.order.idempotency_key, OrderState.SUBMITTED)


class TestCrashRecovery:
    """§8: kill -9 mid-placement, restart, prove we work out whether the order exists.

    The crash is simulated by dropping the router and reopening the store from the same
    file — which is exactly what a restart is from the store's point of view.
    """

    def test_order_that_landed_is_found_and_marked_live(
        self, venue, store, ledger, tmp_path, approved
    ):
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)

        # --- crash here; new process, same database ---
        del router
        with OrderStore(tmp_path / "orders.db") as restarted:
            assert restarted.get(approved.order.idempotency_key).state is OrderState.UNKNOWN
            report = recover_orders(restarted, {str(Venue.KALSHI): venue}, ledger)

            assert report.inspected == 1
            assert report.resolved_live == (approved.order.idempotency_key,)
            assert report.safe_to_trade
            assert (
                restarted.get(approved.order.idempotency_key).state is OrderState.SUBMITTED
            ), "the order does exist at the venue and local state must now say so"

    def test_order_that_never_landed_is_resolved_as_rejected(
        self, venue, store, ledger, tmp_path, approved
    ):
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        venue.timeout_before_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)

        with OrderStore(tmp_path / "orders.db") as restarted:
            report = recover_orders(restarted, {str(Venue.KALSHI): venue}, ledger)
            assert report.resolved_absent == (approved.order.idempotency_key,)
            assert restarted.get(approved.order.idempotency_key).state is OrderState.REJECTED

    def test_a_filled_order_is_recovered_as_filled_not_as_missing(
        self, venue, store, ledger, tmp_path, approved
    ):
        """The nastiest case: it left the book because it *executed*. Treating that as
        'never happened' would lose a real position."""
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)
        venue.fill(approved.order.idempotency_key)

        with OrderStore(tmp_path / "orders.db") as restarted:
            recover_orders(restarted, {str(Venue.KALSHI): venue}, ledger)
            assert restarted.get(approved.order.idempotency_key).state is OrderState.FILLED

    def test_unreachable_venue_during_recovery_halts(
        self, venue, store, ledger, tmp_path, approved
    ):
        """Cannot ask means cannot know. Assuming the order is absent is how a position
        gets doubled on the next signal."""
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)

        venue.reads_fail = True
        with OrderStore(tmp_path / "orders.db") as restarted:
            report = recover_orders(restarted, {str(Venue.KALSHI): venue}, ledger)
            assert not report.safe_to_trade
            assert report.halts
            assert restarted.get(approved.order.idempotency_key).state is OrderState.UNKNOWN

    def test_unconfigured_venue_during_recovery_halts(
        self, store, ledger, tmp_path, approved, venue
    ):
        router = ExecutionRouter(
            {str(Venue.KALSHI): venue}, store, ledger, kill_file=str(tmp_path / "NONE")
        )
        venue.timeout_after_accepting = True
        with pytest.raises(OrderOutcomeUnknown):
            router.submit(approved)

        with OrderStore(tmp_path / "orders.db") as restarted:
            report = recover_orders(restarted, {}, ledger)
            assert not report.safe_to_trade

    def test_clean_shutdown_leaves_nothing_to_recover(self, store, ledger, venue):
        report = recover_orders(store, {str(Venue.KALSHI): venue}, ledger)
        assert report.inspected == 0
        assert report.safe_to_trade


class TestReconciliation:
    def test_matching_books_reconcile(self, venue, ledger):
        venue.set_positions({"FED-26SEP-C025:YES": 100})
        result = reconcile(
            {str(Venue.KALSHI): {"FED-26SEP-C025:YES": 100}},
            {str(Venue.KALSHI): venue},
            ledger,
        )
        assert result.ok

    def test_a_position_the_venue_has_and_we_do_not_is_caught(self, venue, ledger):
        """The dangerous direction. Iterating only over local keys would never see it."""
        venue.set_positions({"SURPRISE:YES": 500})
        result = reconcile({str(Venue.KALSHI): {}}, {str(Venue.KALSHI): venue}, ledger)
        assert not result.ok
        assert result.divergences[0].venue_quantity == 500
        assert result.divergences[0].local_quantity == 0

    def test_a_position_we_have_and_the_venue_does_not_is_caught(self, venue, ledger):
        venue.set_positions({})
        result = reconcile(
            {str(Venue.KALSHI): {"GHOST:YES": 40}}, {str(Venue.KALSHI): venue}, ledger
        )
        assert not result.ok
        assert result.divergences[0].delta == -40

    def test_quantity_mismatch_halts(self, venue, ledger):
        venue.set_positions({"FED:YES": 101})
        result = reconcile(
            {str(Venue.KALSHI): {"FED:YES": 100}}, {str(Venue.KALSHI): venue}, ledger
        )
        assert not result.ok
        assert result.halts

    def test_default_tolerance_is_zero_contracts(self, venue, ledger):
        """A contract is a whole number; there is nothing to round. A dollar tolerance
        would let a hundred missing contracts hide inside three dollars at 3 cents."""
        venue.set_positions({"FED:YES": 100})
        result = reconcile({str(Venue.KALSHI): {"FED:YES": 99}}, {str(Venue.KALSHI): venue}, ledger)
        assert not result.ok

    def test_unreachable_venue_is_not_a_pass(self, venue, ledger):
        venue.reads_fail = True
        result = reconcile({str(Venue.KALSHI): {}}, {str(Venue.KALSHI): venue}, ledger)
        assert not result.ok
        assert result.unreachable == (str(Venue.KALSHI),)

    def test_reconciliation_is_audited(self, venue, ledger):
        venue.set_positions({"FED:YES": 1})
        reconcile({str(Venue.KALSHI): {"FED:YES": 1}}, {str(Venue.KALSHI): venue}, ledger)
        assert len(ledger.entries(kind=EntryKind.RECONCILIATION)) == 1

    def test_cash_divergence_halts(self):
        halt = reconcile_cash(Usd("100"), Usd("95"), tolerance=Usd("0.01"))
        assert halt is not None
        assert halt.limit is Limit.RECONCILIATION_DIVERGED

    def test_cash_within_tolerance_passes(self):
        assert reconcile_cash(Usd("100"), Usd("100.005"), tolerance=Usd("0.01")) is None


class TestRouterRefusesNonApproved:
    def test_a_rejected_decision_cannot_be_executed(self, router, venue):
        rejected = Decision(
            outcome=DecisionOutcome.REJECTED,
            signal_id="s",
            rejection=Rejection(limit=Limit.NO_EDGE, detail="no edge"),
        )
        with pytest.raises(ValueError, match="only APPROVED may execute"):
            router.submit(rejected)
        assert venue.place_calls == []

    def test_a_pending_approval_cannot_be_executed(self, router, venue, approved):
        pending = Decision(
            outcome=DecisionOutcome.PENDING_APPROVAL,
            signal_id="s",
            order=approved.order,
            expires_at=NOW,
            quoted_price=approved.order.limit_price,
        )
        with pytest.raises(ValueError, match="only APPROVED may execute"):
            router.submit(pending)
        assert venue.place_calls == []

    def test_unconfigured_venue_is_refused(self, store, ledger, tmp_path, approved):
        router = ExecutionRouter({}, store, ledger, kill_file=str(tmp_path / "NONE"))
        with pytest.raises(ValueError, match="no trading venue"):
            router.submit(approved)
