import asyncio
import logging

from lona.scheduling import get_current_thread_name
from lona.protocol import DataType
from lona.utils import acquire

AbstractNode = None
TextNode = None
Widget = None
Node = None

_node_classes_setup = False

logger = logging.getLogger('lona.html.document')


def _setup_node_classes():
    # this is necessary to prevent import loops between the Document
    # class and the AbstractNode class and its subclasses

    global AbstractNode, TextNode, Widget, Node, _node_classes_setup

    if _node_classes_setup:
        return

    AbstractNode = acquire('lona.html.abstract_node.AbstractNode')[1]
    TextNode = acquire('lona.html.text_node.TextNode')[1]
    Widget = acquire('lona.html.widget.Widget')[1]
    Node = acquire('lona.html.node.Node')[1]

    _node_classes_setup = True


class LockContextManager:
    def __init__(self, document, context_name):
        self.document = document
        self.context_name = context_name

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.document._release(self.context_name)


class Document:
    def __init__(self, loop=None, default_document=False):
        self.loop = loop
        self.is_default_document = default_document

        self.html = None

        self._locks = [
            # [context_name, started, lock_context_counter],
        ]

        if not self.is_default_document:
            _setup_node_classes()

    def __repr__(self):
        if self.is_default_document:
            return '<DefaultDocument>'

        return '<Document>'

    # locking #################################################################
    async def _await(self, future):
        return await future

    def _release(self, context_name):
        if self.is_default_document or not self.loop:
            return

        for lock in self._locks:
            if lock[0] != context_name:
                continue

            lock[2] -= 1

            if lock[2] == 0:
                self._locks.remove(lock)

            if(self._locks and
               not self._locks[0][1].done() and
               not self._locks[0][1].cancelled()):

                self._locks[0][1].set_result(True)

    def lock(self):
        if self.is_default_document or not self.loop:
            return LockContextManager(self, '')

        context_name = get_current_thread_name()
        context_manager = LockContextManager(self, context_name)
        lock = []

        for _lock in self._locks:
            if _lock[0] == context_name:
                lock = _lock

                break

        if not lock:
            lock = [context_name, asyncio.Future(loop=self.loop), 0]
            self._locks.append(lock)

        started = lock[1]

        if(self._locks.index(lock) == 0 and
           not started.done() and
           not started.cancelled()):

            started.set_result(True)

        lock[2] += 1

        if not started.done() and not started.cancelled():
            asyncio.run_coroutine_threadsafe(
                self._await(started),
                loop=self.loop,
            ).result()

        return context_manager

    # html ####################################################################
    def get_node(self, node_id):
        if isinstance(node_id, Node):
            node_id = node_id.id

        node_id = str(node_id)
        value = [None, [], ]
        widgets = []

        def iter_nodes(node):
            if isinstance(node, Widget):
                widgets.append(node)

            if isinstance(node, (Node, Widget)):
                if node._id == node_id:
                    value[0] = node
                    value[1].extend(widgets)

                    return

                if node.nodes:
                    for i in node.nodes:
                        iter_nodes(i)

            if isinstance(node, Widget):
                widgets.pop()

        with self.lock():
            iter_nodes(self.html)

        return tuple(value)

    def _has_changes(self):
        def has_changes(node):
            if node._has_changes():
                return True

            if hasattr(node, 'nodes'):
                for sub_node in node.nodes:
                    if has_changes(sub_node):
                        return True

            return False

        return has_changes(self.html)

    def _collect_changes(self):
        changes = []

        def add_changes(node):
            if node._has_changes():
                changes.append(node._get_changes())

            if hasattr(node, 'nodes'):
                for sub_node in node.nodes:
                    add_changes(sub_node)

        add_changes(self.html)

        return changes

    def serialize(self):
        if not self.html:
            return self.apply('')

        return DataType.HTML_TREE, self.html._serialize()

    def apply(self, html):
        # HTML update
        if html is self.html:
            if not self._has_changes():
                return

            changes = self._collect_changes()
            self.html._clear_changes()

            return DataType.HTML_UPDATE, changes

        # HTML
        else:
            if hasattr(self.html, 'document'):
                self.html.document = None

            # prepare html
            if isinstance(html, str):
                if '<' not in html:
                    html = TextNode(html)

                else:
                    self.html = html

                    return DataType.HTML, html

            self.html = html

            self.html.document = self
            self.html._clear_changes()

            return self.serialize()