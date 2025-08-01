from __future__ import annotations

import os
from collections.abc import Sequence

import pytest

from xdist.remote import Producer
from xdist.report import report_collection_diff
from xdist.workermanage import parse_tx_spec_config
from xdist.workermanage import WorkerController


class EachScheduling:
    """Implement scheduling of test items on all nodes.

    If a node gets added after the test run is started then it is
    assumed to replace a node which got removed before it finished
    its collection.  In this case it will only be used if a node
    with the same spec got removed earlier.

    Any nodes added after the run is started will only get items
    assigned if a node with a matching spec was removed before it
    finished all its pending items.  The new node will then be
    assigned the remaining items from the removed node.
    """

    def __init__(self, config: pytest.Config, log: Producer | None = None) -> None:
        self.config = config
        self.numnodes = len(parse_tx_spec_config(config))
        self.node2collection: dict[WorkerController, list[str]] = {}
        self.node2pending: dict[WorkerController, list[int]] = {}
        self._started: list[WorkerController] = []
        self._removed2pending: dict[WorkerController, list[int]] = {}
        if log is None:
            self.log = Producer("eachsched")
        else:
            self.log = log.eachsched
        self.collection_is_completed = False
        self._allowed_workers = self._parse_allowed_workers()

    def _parse_allowed_workers(self) -> set[str]:
        """Parse XDIST_ONLY environment variable to get allowed worker IDs."""
        xdist_only = os.environ.get("XDIST_ALLOWED_WORKERS", "").strip()
        if not xdist_only:
            return set()  # Empty set means all workers are allowed
        
        worker_ids = []
        for worker_id in xdist_only.split(","):
            worker_id = worker_id.strip()
            if worker_id:
                worker_ids.append(worker_id)
        
        return set(worker_ids)

    def _is_allowed_worker(self, node: WorkerController) -> bool:
        """Check if a worker is allowed to run tests based on XDIST_ONLY."""
        if not self._allowed_workers:  # Empty set means all workers allowed
            return True
        return node.gateway.id in self._allowed_workers

    @property
    def nodes(self) -> list[WorkerController]:
        """A list of all nodes in the scheduler."""
        return list(self.node2pending.keys())

    @property
    def tests_finished(self) -> bool:
        if not self.collection_is_completed:
            return False
        if self._removed2pending:
            return False
        for pending in self.node2pending.values():
            if len(pending) >= 2:
                return False
        return True

    @property
    def has_pending(self) -> bool:
        """Return True if there are pending test items.

        This indicates that collection has finished and nodes are
        still processing test items, so this can be thought of as
        "the scheduler is active".
        """
        for pending in self.node2pending.values():
            if pending:
                return True
        return False

    def add_node(self, node: WorkerController) -> None:
        assert node not in self.node2pending
        self.node2pending[node] = []

    def add_node_collection(
        self, node: WorkerController, collection: Sequence[str]
    ) -> None:
        """Add the collected test items from a node.

        Collection is complete once all nodes have submitted their
        collection.  In this case its pending list is set to an empty
        list.  When the collection is already completed this
        submission is from a node which was restarted to replace a
        dead node.  In this case we already assign the pending items
        here.  In either case ``.schedule()`` will instruct the
        node to start running the required tests.
        """
        assert node in self.node2pending
        if not self.collection_is_completed:
            self.node2collection[node] = list(collection)
            self.node2pending[node] = []
            if len(self.node2collection) >= self.numnodes:
                self.collection_is_completed = True
        elif self._removed2pending:
            for deadnode in self._removed2pending:
                if deadnode.gateway.spec == node.gateway.spec:
                    dead_collection = self.node2collection[deadnode]
                    if collection != dead_collection:
                        msg = report_collection_diff(
                            dead_collection,
                            collection,
                            deadnode.gateway.id,
                            node.gateway.id,
                        )
                        self.log(msg)
                        return
                    pending = self._removed2pending.pop(deadnode)
                    self.node2pending[node] = pending
                    break

    def mark_test_complete(
        self, node: WorkerController, item_index: int, duration: float = 0
    ) -> None:
        self.node2pending[node].remove(item_index)

    def mark_test_pending(self, item: str) -> None:
        raise NotImplementedError()

    def remove_pending_tests_from_node(
        self,
        node: WorkerController,
        indices: Sequence[int],
    ) -> None:
        raise NotImplementedError()

    def remove_node(self, node: WorkerController) -> str | None:
        # KeyError if we didn't get an add_node() yet
        pending = self.node2pending.pop(node)
        if not pending:
            return None
        crashitem = self.node2collection[node][pending.pop(0)]
        if pending:
            self._removed2pending[node] = pending
        return crashitem

    def schedule(self) -> None:
        """Schedule the test items on the nodes.

        If the node's pending list is empty it is a new node which
        needs to run all the tests.  If the pending list is already
        populated (by ``.add_node_collection()``) then it replaces a
        dead node and we only need to run those tests.
        
        When XDIST_ALLOWED_WORKERS environment variable is set, only workers 
        specified in that variable will run tests, others remain idle.
        """
        assert self.collection_is_completed
        for node, pending in self.node2pending.items():
            if node in self._started:
                continue
            
            # Check if this worker is allowed to run tests
            if not self._is_allowed_worker(node):
                # Mark as started but don't send any tests, just shutdown
                node.shutdown()
                self._started.append(node)
                continue
                
            if not pending:
                pending[:] = range(len(self.node2collection[node]))
                node.send_runtest_all()
                node.shutdown()
            else:
                node.send_runtest_some(pending)
            self._started.append(node)
