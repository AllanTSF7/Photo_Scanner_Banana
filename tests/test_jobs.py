from banana import db, jobs
from banana.models import Job


def test_retry_then_fail(tmp_path):
    engine = db.make_engine(tmp_path / "t.db")
    calls = []

    @jobs.handler("flaky")
    def _flaky(session, job):
        calls.append(job.id)
        raise RuntimeError("boom")

    @jobs.handler("ok")
    def _ok(session, job):
        calls.append(job.id)

    with db.session(engine) as s:
        flaky = jobs.enqueue(s, "flaky")
        ok = jobs.enqueue(s, "ok")

    while (job_id := jobs.claim_next(engine)) is not None:
        jobs.run_one(engine, job_id)

    with db.session(engine) as s:
        assert s.get(Job, ok.id).state == "done"
        failed = s.get(Job, flaky.id)
        assert failed.state == "failed" and failed.attempts == jobs.MAX_ATTEMPTS and "boom" in failed.error
    assert calls.count(flaky.id) == jobs.MAX_ATTEMPTS
