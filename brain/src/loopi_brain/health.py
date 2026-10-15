"""`loopi-brain worker-health`: is a pipeline worker alive?

Docker runs this every 30 s inside the worker's container, so it imports nothing but psycopg
(no numpy, Procrastinate or cryptography): its CPU time counts against the worker.
"""
import psycopg

HEARTBEAT_MAX_AGE_S = 60


def worker_alive(database_url: str, max_age_s: int = HEARTBEAT_MAX_AGE_S) -> bool:
    """True if some worker sent a Procrastinate heartbeat in the last ``max_age_s`` seconds."""
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - make_interval(secs => %s)",
            (max_age_s,),
        ).fetchone()
    return row[0] > 0
