from lisan.tools.jobs import _JOB_HANDLERS, JOB_TYPES


def test_every_declared_job_type_has_exactly_one_handler():
    assert set(_JOB_HANDLERS) == set(JOB_TYPES)
