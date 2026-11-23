"""Chengqiao night-shift weighing reconciliation.

Two independent SQLite stores implement the business flow:

* :class:`chengqiao.process_store.ProcessStore` -- frozen named rules,
  accepted orders, traversal state and the processing-end submission.
* :class:`chengqiao.account_store.AccountStore` -- declaration of submitted
  receipts, individually posted endpoint pieces, and the sealed result.

The two stores never share a connection or a transaction.  Everything the
account end needs is carried in the receipt/piece payloads produced by the
processing end.
"""

__version__ = "1.0.0"
