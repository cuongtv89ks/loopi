"""Evaluation of the AI pipeline against the owner's golden set (spec §13, D21).

The golden set itself — clips, keyframe copies, reference transcripts, questions — lives in the brain's data dir
(``LOOPI_EVAL_DIR``) and database, never in git; this package holds only the code. Logs name samples by id and
carry counts, never their text.
"""
