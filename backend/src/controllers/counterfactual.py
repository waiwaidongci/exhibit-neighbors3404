from ..services import counterfactual_jobs as jobs


class CounterfactualController:
    list = staticmethod(jobs.list_jobs)
    create = staticmethod(jobs.create_job)
    detail = staticmethod(jobs.detail)
    resume = staticmethod(jobs.resume_job)
    verify = staticmethod(jobs.verification)
    options = staticmethod(jobs.options)
